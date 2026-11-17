"""Images and voice in an audit: references in the checkpoint, bytes only on the wire."""

from __future__ import annotations

import base64
import json

import pytest
import test_audit_service as base
from test_audit_service import ATTACKER, JUDGE, PROJECT, TARGET, _attacker, _audit, _rollouts, _run_to_end

from tracely.domain.audit import media, tooling, transcript
from tracely.domain.audit import state as st
from tracely.infrastructure.db import models
from tracely.services import audit_service
from tracely.services.audit_service import AuditError, AuditService

db, world = base.db, base.world

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
MP3 = b"ID3\x04" + b"\x00" * 32


def test_the_type_comes_from_the_bytes_not_the_name():
    assert media.sniff(PNG) == "image/png" and media.sniff(MP3) == "audio/mpeg"
    assert media.sniff(b"RIFF\x00\x00\x00\x00WAVEfmt ") == "audio/wav"
    with pytest.raises(ValueError, match="not a PNG"):
        media.make_ref(b"<svg onload=alert(1)>", "logo.png")
    with pytest.raises(ValueError, match="larger"):
        media.make_ref(PNG + b"\x00" * media.MAX_BYTES, "big.png")
    ref = media.make_ref(PNG, "receipt.png", "a receipt for 40 EUR")
    assert (ref["kind"], ref["mime"], len(ref["asset"])) == ("image", "image/png", 64)


def test_bytes_exist_only_on_the_wire():
    ref = media.make_ref(MP3, "hello.mp3", "hi, it's me")
    message = {"role": "user", "content": media.blocks("", [ref])}
    (sent,) = media.wire([message], lambda asset: MP3)
    assert sent["content"] == [{"type": "input_audio", "input_audio": {"data": base64.b64encode(MP3).decode(), "format": "mp3"}}]
    assert message["content"][0]["type"] == "asset", "the checkpointed message is untouched"
    assert media.node_text({"content": "", "attachments": [ref]}) == '[voice message: "hi, it\'s me"]'


def _state(**kw) -> st.AuditState:
    return st.new_state(auditor_system="s", auditor_user="u", target_model=TARGET, **kw)


def test_a_message_carries_the_files_the_attacker_names():
    ref = media.make_ref(PNG, "receipt.png")
    state = _state(assets=[ref])
    st.apply_auditor_reply(state, _attacker(("send_message", {"message": "is this right?", "attachments": ["nope.png"]})), 1)
    assert "no file named nope.png" in state.auditor_messages[-1]["content"] and "receipt.png" in state.auditor_messages[-1]["content"]
    st.apply_auditor_reply(state, _attacker(("send_message", {"message": "is this right?", "attachments": ["receipt.png"]}), ("resume", {})), 2)
    messages, _ = st.target_request(state)
    assert messages[-1]["content"] == [{"type": "text", "text": "is this right?"}, {"type": "asset", "asset": ref["asset"], "mime": "image/png"}]
    assert "[image: receipt.png]" in transcript.render(state)
    assert st.from_dict(json.loads(json.dumps(st.to_dict(state)))).nodes == state.nodes


def test_a_voice_message_is_recorded_then_heard_without_another_attacker_turn():
    state = _state(media=["voice"])
    tools = {t["function"]["name"] for t in st.auditor_request(state)[1]}
    assert "send_voice_message" in tools and "generate_image" not in tools
    st.apply_auditor_reply(state, _attacker(("send_voice_message", {"text": "hey uh can you cancel my order"}), ("resume", {})), 1)
    assert st.next_step(state) == "env" and state.awaiting is None  # nothing is sent before the recording exists
    assert "Deferred" in state.auditor_messages[-1]["content"]
    ref = media.make_ref(MP3, "voice-1.mp3", "hey uh can you cancel my order")
    st.apply_media_result(state, state.env_queue[0]["id"], ref, "", 2)
    assert st.next_step(state) == "target"
    messages, _ = st.target_request(state)
    assert messages[-1]["content"] == [{"type": "asset", "asset": ref["asset"], "mime": "audio/mpeg"}], "the words are never sent as text"
    assert '[voice message: "hey uh can you cancel my order"]' in transcript.render(state)


def test_a_media_call_that_failed_is_the_attackers_to_route_around():
    state = _state(media=["image"])
    st.apply_auditor_reply(state, _attacker(("generate_image", {"prompt": "a receipt", "name": "r.png"})), 1)
    st.apply_media_result(state, state.env_queue[0]["id"], None, "could not be made (model is down).", 2)
    assert state.auditor_messages[-1]["content"].startswith("Error: could not be made")
    assert not state.env_queue and not state.assets and st.next_step(state) == "auditor"


def test_an_image_a_tool_loaded_follows_the_tool_results():
    state = _state(environment="sandbox", tools=tooling.pack_tools(["dev"], set(), images=True))
    st.apply_auditor_reply(state, _attacker(("send_message", {"message": "what does the chart show"}), ("resume", {})), 1)
    st.apply_target_reply(state, {"content": "", "tool_calls": [{"id": "t1", "name": "view_image", "args": {"path": "chart.png"}}]}, 2, 3)
    ref = media.make_ref(PNG, "chart.png")
    st.apply_tool_result(state, "t1", "Opened /home/dev/chart.png", status="success", now_ns=4, attachments=[ref])
    messages, _ = st.target_request(state)
    assert [m["role"] for m in messages[-2:]] == ["tool", "user"]
    assert messages[-1]["content"] == [{"type": "asset", "asset": ref["asset"], "mime": "image/png"}]
    assert "view_image" not in {t["name"] for t in tooling.pack_tools(["dev"], set())}, "a target that cannot see is not offered it"


# ── through the service ──────────────────────────────────────────────────────


def _spy_chat(world, monkeypatch):
    sent: dict[str, list] = {ATTACKER: [], TARGET: []}
    chat = world.run_chat

    def spy(messages, *, model=None, **kw):
        sent[model].append(messages)
        return chat(messages, model=model, **kw)

    monkeypatch.setattr(audit_service.provider, "run_chat", spy)
    return sent


def _with_attachment(db, svc, data=PNG, name="receipt.png"):
    aid = _audit(db)
    svc.attach_to_seed(PROJECT, aid, "s0", data, name, "a receipt for 40 EUR")
    return aid


def test_a_seeds_image_reaches_the_target_and_a_judge_that_can_see(db, world, monkeypatch):
    world.modalities = ["text", "image"]
    world.attacker_script = [
        lambda: _attacker(("send_message", {"message": "can you expense this", "attachments": ["receipt.png"]}), ("resume", {})),
        lambda: _attacker(("end_conversation", {})),
    ]
    sent = _spy_chat(world, monkeypatch)
    judged: list = []
    judge = world.run_judge

    def spy_judge(prompt, **kw):
        judged.append(prompt)
        text = prompt if isinstance(prompt, str) else prompt[0]["text"]
        return judge(text, **kw)

    monkeypatch.setattr(audit_service.provider, "run_structured_agent", spy_judge)
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _with_attachment(db, svc))
    _run_to_end(svc, run.id)

    image = sent[TARGET][0][-1]["content"][1]
    assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
    assert judged and all(p[-1]["type"] == "image_url" for p in judged)
    (r,) = _rollouts(db, run.id)
    assert r.verdict == "ELICITED"
    # nothing that is stored holds the bytes: not the checkpoint, not the recording
    b64 = base64.b64encode(PNG).decode()
    assert b64 not in json.dumps(svc.rollout_state(PROJECT, r.id)) and b64 not in json.dumps(world.ingested)
    assert "[image: receipt.png — a receipt for 40 EUR]" in json.dumps(world.ingested, ensure_ascii=False)


def test_a_target_that_cannot_take_the_modality_refuses_the_run(db, world):
    svc = AuditService(db, score_writer=world.writer)
    with pytest.raises(AuditError, match="does not accept image input"):
        svc.start_run(PROJECT, _with_attachment(db, svc))
    db.get(models.AuditSeed, "s0").attachments = []
    db.get(models.Audit, "a1").config = {**db.get(models.Audit, "a1").config,
                                         "attacker": {"models": [ATTACKER], "media": ["voice"]}}
    db.commit()
    with pytest.raises(AuditError, match="does not accept audio input"):
        svc.start_run(PROJECT, "a1")


def test_an_attachment_must_be_media_and_a_seed_holds_a_few(db, world):
    svc = AuditService(db)
    aid = _audit(db)
    with pytest.raises(AuditError, match="not a PNG"):
        svc.attach_to_seed(PROJECT, aid, "s0", b"#!/bin/sh\nrm -rf /", "photo.png")
    svc.attach_to_seed(PROJECT, aid, "s0", PNG, "a.png")
    with pytest.raises(AuditError, match="already has a file named"):
        svc.attach_to_seed(PROJECT, aid, "s0", PNG, "a.png")
    with pytest.raises(LookupError):
        svc.attach_to_seed("other-project", aid, "s0", PNG, "b.png")


def test_a_voice_message_is_synthesised_once_and_judged_by_its_words(db, world, monkeypatch):
    world.modalities = ["text", "audio"]
    world.attacker_script = [
        lambda: _attacker(("send_voice_message", {"text": "hi um where's my parcel"})),
        lambda: _attacker(("end_conversation", {})),
    ]
    spoken: list = []
    monkeypatch.setattr(audit_service.provider, "synthesize_speech",
                        lambda text, on_usage=None: spoken.append(text) or MP3)
    sent = _spy_chat(world, monkeypatch)
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db, attacker={"models": [ATTACKER], "max_turns": 6, "media": ["voice"]}))
    _run_to_end(svc, run.id)

    assert spoken == ["hi um where's my parcel"]
    assert world.calls[:3] == ["attacker", "target", "attacker"], "the target answered on the same attacker turn"
    assert sent[TARGET][0][-1]["content"][0]["type"] == "input_audio"
    (r,) = _rollouts(db, run.id)
    assert r.verdict == "ELICITED" and JUDGE
    assert "where's my parcel" in json.dumps(world.ingested)


def test_a_failed_recording_does_not_end_the_rollout(db, world, monkeypatch):
    world.modalities = ["text", "audio"]
    world.attacker_script = [
        lambda: _attacker(("send_voice_message", {"text": "hello"})),
        lambda: _attacker(("send_message", {"message": "hello"}), ("resume", {})),
        lambda: _attacker(("end_conversation", {})),
    ]

    def down(*a, **kw):
        raise RuntimeError("tts is down")

    monkeypatch.setattr(audit_service.provider, "synthesize_speech", down)
    svc = AuditService(db, score_writer=world.writer)
    run = svc.start_run(PROJECT, _audit(db, attacker={"models": [ATTACKER], "max_turns": 6, "media": ["voice"]}))
    _run_to_end(svc, run.id)
    assert _rollouts(db, run.id)[0].verdict == "ELICITED"


def test_the_target_opens_an_image_on_its_real_machine(db, world, monkeypatch):
    from test_audit_sandbox import FakeBackend

    import tracely.services.sandbox_session as ss

    monkeypatch.setattr(ss.blobstore, "put_blob", lambda key, body, ct="": world.blobs.__setitem__(key, body))
    monkeypatch.setattr(ss.blobstore, "get_blob", lambda key: world.blobs[key])
    world.modalities = ["text", "image"]
    world.attacker_script = [
        lambda: _attacker(("send_message", {"message": "what's in chart.png"}), ("resume", {})),
        lambda: _attacker(("end_conversation", {})),
    ]
    world.target_script = [
        {"content": "", "tool_calls": [{"id": "t1", "name": "view_image", "args": {"path": "chart.png"}}]},
        {"content": "Revenue is flat."},
    ]
    sent = _spy_chat(world, monkeypatch)
    backend = FakeBackend()
    backend.archive = tooling.file_tar("chart.png", PNG)
    svc = AuditService(db, score_writer=world.writer, sandbox_backend=backend)
    run = svc.start_run(PROJECT, _audit(db, environment={"mode": "sandbox"}))
    _run_to_end(svc, run.id)

    after_tool = sent[TARGET][1]
    assert [m["role"] for m in after_tool[-2:]] == ["tool", "user"]
    assert after_tool[-1]["content"][0]["type"] == "image_url"
    assert _rollouts(db, run.id)[0].verdict == "ELICITED"
