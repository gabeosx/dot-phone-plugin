// Per-installation source context. No owner, mailbox or exclusion is bundled.
export function sourceConfig(config) {
  const exclude_terms=config.exclude_terms??[];
  if(!Array.isArray(exclude_terms)||exclude_terms.some(t=>typeof t!=='string'||!t.trim())||
     (config.gmail_account!==undefined&&(typeof config.gmail_account!=='string'||!config.gmail_account.includes('@'))))
    throw Error('invalid_source_context');
  return {...config,exclude_terms};
}
export function verifyGmailProfile(profile,expected) {
  const actual=profile?.email_address??profile?.email;
  if(typeof actual!=='string'||!actual.includes('@'))throw Error('gmail_identity_unavailable');
  if(expected!==undefined&&actual.toLowerCase()!==expected.toLowerCase())throw Error('gmail_account_mismatch');
  return actual;
}
