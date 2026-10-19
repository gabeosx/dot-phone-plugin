// Runtime owned by one local subscription agent; no provider keys or APIs.
import fs from 'node:fs/promises';
import {constants} from 'node:fs';
import crypto from 'node:crypto';
import path from 'node:path';

async function readPrivate(file) {
  const handle = await fs.open(file, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    if ((await handle.stat()).size > 8192) throw Error('oversize_worker_payload');
    return await handle.readFile('utf8');
  } finally { await handle.close(); }
}

async function writePrivate(file, value) {
  const temporary = path.join(path.dirname(file), '.worker-' + crypto.randomUUID());
  try {
    await fs.writeFile(temporary, JSON.stringify(value), {mode: 0o600, flag: 'wx'});
    await fs.rename(temporary, file);
  } finally { await fs.unlink(temporary).catch(() => {}); }
}

export class LocalGmailSubscriptionWorker {
  static async create(directory, identity) {
    if (!path.isAbsolute(directory) || (await fs.lstat(directory)).isSymbolicLink()) throw Error('invalid_worker_directory');
    const raw = await readPrivate(path.join(directory, 'registry.json'));
    const registry = JSON.parse(raw);
    if (registry.schema !== 1 || !Array.isArray(registry.cases) || registry.cases.length > 4 || !registry.cases.length) throw Error('invalid_registry');
    for (const c of registry.cases) if (!/^[a-f0-9]{32}$/.test(c.call_id) || !/^"dots-phone-gmail-probe-[a-f0-9]{32}"$/.test(c.query) || c.revision !== 1) throw Error('invalid_registry');
    const worker = new LocalGmailSubscriptionWorker();
    Object.assign(worker, {directory, identity, cases: registry.cases, registryHash: crypto.createHash('sha256').update(raw).digest('hex'),
      processed: new Set(), pending: null, reports: [], sessionDeadline: Date.now() + 30 * 60 * 1000});
    return worker;
  }

  async heartbeat(serving) {
    await writePrivate(path.join(this.directory, 'worker-ready.json'), {schema: 1, serving, registry_hash: this.registryHash,
      heartbeat_ms: Date.now(), worker_identity: this.identity, completed_queries: this.reports.length});
  }

  async waitNext(timeoutMs = 60000) {
    if (this.pending) throw Error('lookup_already_pending');
    const deadline = Math.min(this.sessionDeadline, Date.now() + Math.min(timeoutMs, 60000));
    while (Date.now() < deadline) {
      if (this.processed.size >= this.cases.length || await fs.stat(path.join(this.directory, 'finished.signal')).then(() => true, () => false)) {
        await this.heartbeat(false); return {status: 'finished'};
      }
      // This heartbeat is published from the actual serving loop, not initialization.
      await this.heartbeat(true);
      for (const c of this.cases) {
        if (this.processed.has(c.call_id)) continue;
        let request;
        try { request = JSON.parse(await readPrivate(path.join(this.directory, c.call_id + '-request.json'))); }
        catch (error) { if (error.code === 'ENOENT') continue; throw Error('invalid_worker_request'); }
        if (Object.keys(request).sort().join(',') !== ['schema','call_id','request_id','revision','nonce','query','created_ms','expires_ms'].sort().join(',') ||
            request.schema !== 1 || request.call_id !== c.call_id || request.revision !== c.revision || request.query !== c.query ||
            !/^[a-f0-9]{32}$/.test(request.request_id) || !/^[a-f0-9]{64}$/.test(request.nonce) ||
            !Number.isFinite(request.created_ms) || !Number.isFinite(request.expires_ms) ||
            request.expires_ms <= Date.now() || request.expires_ms - request.created_ms > 31000 || request.created_ms > Date.now() + 1000) throw Error('invalid_worker_request');
        this.pending = {...request, observed_ms: Date.now()};
        // The one-use capability stays in Node memory and its private source file.
        return {status: 'request', call_id: request.call_id, request_id: request.request_id, query: request.query,
          created_ms: request.created_ms, expires_ms: request.expires_ms, observed_ms: this.pending.observed_ms};
      }
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    if (Date.now() >= this.sessionDeadline) {await this.heartbeat(false); return {status: 'finished'};}
    return {status: 'waiting'};
  }

  async publish(count, startedMs, endedMs) {
    const request = this.pending;
    if (!request || ![0, 1].includes(count) || !Number.isFinite(startedMs) || !Number.isFinite(endedMs) ||
        startedMs < request.observed_ms || endedMs < startedMs || endedMs > Date.now() || Date.now() >= request.expires_ms) throw Error('invalid_or_expired_lookup');
    const written = Date.now();
    const result = {schema: 1, call_id: request.call_id, request_id: request.request_id, revision: request.revision, nonce: request.nonce,
      status: count ? 'matches' : 'no_matches', match_count: count, request_observed_ms: request.observed_ms,
      plugin_started_ms: startedMs, plugin_finished_ms: endedMs, result_written_ms: written};
    await writePrivate(path.join(this.directory, request.call_id + '-result.json'), result);
    const report = {call_id: request.call_id, request_id: request.request_id, gmail_lookup_count: 1, match_count: count,
      request_observed_ms: request.observed_ms, plugin_started_ms: startedMs, plugin_finished_ms: endedMs, result_written_ms: written};
    this.reports.push(report); this.processed.add(request.call_id); this.pending = null;
    await writePrivate(path.join(this.directory, 'worker-results.json'), {schema: 1, identity: this.identity, reports: this.reports});
    return report;
  }

  async stop() {this.pending = null; await this.heartbeat(false); return {identity: this.identity, completed_queries: this.reports.length, reports: this.reports};}
}
