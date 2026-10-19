import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from xml.etree import ElementTree

import pytest
from starlette.websockets import WebSocketState

from dots_phone.carrier_stream import CarrierStream, reservation_menu_option


class Socket:
    def __init__(self):
        self.headers = {}
        self.application_state = self.client_state = WebSocketState.CONNECTED
        self.input = asyncio.Queue()
        self.sent = []

    async def receive(self):
        return await self.input.get()

    async def send_text(self, data):
        self.sent.append(json.loads(data))

    async def close(self, code=1000):
        self.application_state = self.client_state = WebSocketState.DISCONNECTED


@pytest.mark.parametrize(
    "digit,quote,heard,expected",
    [
        ("5", "Reservations are option five", "For existing bookings, use five.", True),
        ("2", "", "Press one for bookings. Press two for account information.", True),
        ("0", "Operator", "To speak with a person, use zero.", True),
        ("2", "Made-up model wording", "A new recipient menu.", True),
        ("25", "For reservations press twenty five", "For reservations press twenty five", False),
        ("*", "Press star for reservations", "Press star for reservations", True),
        ("５", "For reservations press five", "For reservations press five", False),
        ("2", "Press two", "", False),
        ("2", "Press two", "   ", False),
        ("2", "Press two", None, False),
    ],
)
def test_menu_control_bounds_without_a_parallel_language_classifier(digit, quote, heard, expected):
    assert reservation_menu_option(digit, quote, heard) is expected


async def test_requested_replacement_keeps_transport_and_drops_gap_speech():
    first, second = Socket(), Socket()
    stream = CarrierStream(first, "MZfirst")
    original = stream
    rebinding = []
    stream.on_rebind = rebinding.append
    nonce = stream.begin_change()
    await stream.send_text('{"event":"media","media":{"payload":"gap"}}')
    first.input.put_nowait({"type": "websocket.disconnect"})
    await stream.active.detached.wait()
    assert not stream.closed
    lease = await stream.attach(second, "MZsecond", nonce)
    await stream.wait_change(0.1)
    assert stream is original and stream.stream_count == 2 and rebinding == ["MZsecond"]
    await stream.send_text('{"event":"media","streamSid":"MZfirst","media":{"payload":"new"}}')
    assert not first.sent and second.sent[0]["streamSid"] == "MZsecond"
    assert second.sent[0]["media"]["payload"] == "new"
    second.input.put_nowait({"type": "websocket.receive", "text": '{"event":"media"}'})
    assert (await stream.receive())["text"] == '{"event":"media"}'
    await stream.close()
    assert lease.detached.is_set()


async def test_replacement_nonce_is_single_use_and_unrequested_drop_is_terminal():
    stream = CarrierStream(Socket(), "MZfirst")
    with pytest.raises(ValueError):
        await stream.attach(Socket(), "MZforeign", "invented")
    nonce = stream.begin_change()
    with pytest.raises(ValueError):
        await stream.attach(Socket(), "MZforeign", "wrong")
    second = Socket()
    await stream.attach(second, "MZsecond", nonce)
    with pytest.raises(ValueError):
        await stream.attach(Socket(), "MZthird", nonce)
    second.input.put_nowait({"type": "websocket.disconnect"})
    assert (await asyncio.wait_for(stream.receive(), 0.2))["type"] == "websocket.disconnect"
    assert stream.closed
    with pytest.raises(ValueError):
        await stream.attach(Socket(), "MZthird", nonce)
    await stream.close()


async def test_missing_replacement_ends_instead_of_replaying_or_redialing():
    socket = Socket()
    stream = CarrierStream(socket, "MZfirst")
    stream.begin_change()
    with pytest.raises(TimeoutError):
        await stream.wait_change(0.01)
    assert stream.closed and not socket.sent
    assert socket.client_state == WebSocketState.DISCONNECTED


async def test_carrier_control_updates_existing_call_and_requires_audio_reattachment(
    settings, service, brief
):
    pytest.importorskip("pipecat")
    from dots_phone.pipecat_voice import PipecatTelephony
    from tests.conftest import started

    settings.pipecat_owned_dtmf = True
    settings.dry_run = False
    cid, _ = await started(service, brief)
    call = service.call(settings.owner, cid)
    call["state"] = "active"
    sid = call["provider_sid"]
    service.save(call)
    bridge = SimpleNamespace(schema=4, registered=lambda _: True, scope=MagicMock())
    service.local_context_worker = bridge
    stream = CarrierStream(Socket(), "MZfirst")
    llm = SimpleNamespace(
        dialog=[{"role": "user", "text": "For reservations, press two."}],
        clear_playback=AsyncMock(),
    )
    session = SimpleNamespace(
        carrier=stream, ending=False, llm=llm, menu_lock=asyncio.Lock(), used_menus=set()
    )
    client = MagicMock()
    provider = PipecatTelephony(settings, service, client)
    provider.sessions[sid] = session
    accepted = []

    def update(**kwargs):
        xml = ElementTree.fromstring(kwargs["twiml"])
        accepted.append(xml)
        return SimpleNamespace(sid=sid)

    client.calls.return_value.update.side_effect = update

    async def reconnect():
        while not accepted:
            await asyncio.sleep(0.001)
        parameters = {
            p.attrib["name"]: p.attrib["value"]
            for p in accepted[0].findall("./Connect/Stream/Parameter")
        }
        assert parameters["dots_call_id"] == cid
        await stream.attach(Socket(), "MZsecond", parameters["dots_reattach_nonce"])

    receiver = asyncio.create_task(reconnect())
    result = await provider.press_phone_key(cid, "2", "For reservations, press two.")
    await receiver
    assert result["audio_reconnected"] and session.llm is llm
    assert accepted[0].find("Play").attrib["digits"] == "2"
    client.calls.create.assert_not_called()
    assert client.calls.call_args.args == (sid,)
    assert service.call(settings.owner, cid)["usage"]["carrier_menu_controls"][0]["reconnected"]
    from dots_phone.service import Refused

    with pytest.raises(Refused):
        await provider.press_phone_key(cid, "2", "For reservations, press two.")
    assert client.calls.return_value.update.call_count == 1
    await stream.close()
