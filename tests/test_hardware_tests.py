"""Hardware probe utilities can be validated without touching a robot."""
from types import SimpleNamespace as NS
import wave
import pytest
from cozmo_roverd import hardware_tests


def test_probe_cases_cover_stock_audio_lights_and_baselines():
    assert "baseline-idle" in hardware_tests.CASES
    assert "baseline-camera" in hardware_tests.CASES
    assert "baseline-animation" in hardware_tests.CASES
    assert "baseline-animation-camera" in hardware_tests.CASES
    assert "backpack-stock" in hardware_tests.CASES
    assert "backpack-side" in hardware_tests.CASES
    assert "backpack-side-camera" in hardware_tests.CASES
    assert "charger" in hardware_tests.CASES
    assert "audio-idle" in hardware_tests.CASES
    assert "audio-camera" in hardware_tests.CASES


def test_audio_probe_generates_correct_robot_playback_wav(tmp_path):
    wav = tmp_path / "tone.wav"
    hardware_tests._tone_wav(wav)
    with wave.open(str(wav), "rb") as stream:
        assert stream.getnchannels() == 1
        assert stream.getsampwidth() == 2
        assert stream.getframerate() == 22050
        assert stream.getnframes() > 0


def test_physical_probe_refuses_to_start_without_explicit_risk_flag():
    with pytest.raises(SystemExit) as exc:
        hardware_tests.main(["--case", "audio-idle"])
    assert exc.value.code == 2


def test_probe_reads_ack_window_without_patching_transport():
    from threading import Lock
    from queue import Queue
    cli = NS(
        conn=NS(
            send_thread=NS(lock=Lock(), window=NS(
                next_seq=12, expected_seq=5, max_seq=65536),
                queue=Queue(), outgoing_packets=8, sent_packets=12,
                sent_frames=6, discarded_frames=0),
            recv_thread=NS(received_frames=40, received_packets=50)),
        num_audio_frames_played=3, last_image_timestamp=99, robot_status=12)
    result = hardware_tests._snapshot(cli)
    assert result["pending_reliable_packets"] == 7
    assert result["next_ack_expected"] == 5
    assert result["audio_frames_reported_played"] == 3
    assert result["received_packets"] == 50


def test_probe_stock_backpack_and_side_cases_send_expected_packets(tmp_path):
    from pycozmo.protocol_encoder import LightStateSide, LightStateCenter
    sent = []
    client = NS(
        conn=NS(send=sent.append),
        set_all_backpack_lights=lambda color: sent.append(("stock_backpack", color)))
    rec = NS(event=lambda *args, **kwargs: None)
    action = hardware_tests._apply_case(client, "backpack-side", rec, tmp_path)
    assert "LightStateSide" in action
    assert len(sent) == 1
    assert isinstance(sent[-1], LightStateSide)
    sent.clear()
    action = hardware_tests._apply_case(client, "backpack-stock", rec, tmp_path)
    assert "stock_set_all_backpack_lights" in action
    assert sent[-1][0] == "stock_backpack"


def test_result_summary_reports_exact_failure_and_transport_counters(tmp_path):
    import json
    samples = [
        {"event": "pre_command_transport", "transport": {
            "next_ack_expected": 2, "audio_frames_reported_played": 0}},
        {"event": "command_issued"},
        {"event": "stalled", "transport": {
            "next_ack_expected": 7, "pending_reliable_packets": 62,
            "queued_app_commands": 202, "received_frames": 333,
            "audio_frames_reported_played": 62}},
    ]
    (tmp_path / "measurements.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in samples))
    result = hardware_tests._summarize(tmp_path, 3)
    assert result["status"] == "FAIL"
    assert "ACK progress" in result["reason"]
    assert result["pending"] == 62
    assert result["queued"] == 202
    assert result["ack_before"] == 2
    assert result["ack_after"] == 7
    assert result["audio_frames"] == 62


def test_one_command_runner_stops_and_restores_existing_service(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(hardware_tests, "_service_active", lambda: True if not calls else False)
    monkeypatch.setattr(hardware_tests, "_service_action", lambda action: calls.append(action))
    monkeypatch.setattr(hardware_tests, "_run_case",
                        lambda case, args, directory: {
                            "status": "PASS", "reason": "completed"})
    assert hardware_tests.main([
        "--case", "baseline-camera", "--i-understand-risk",
        "--output-dir", str(tmp_path)]) == 0
    assert calls == ["stop", "start"]


def test_default_suite_contains_actual_audio_animation_and_light_controls():
    assert hardware_tests.DEFAULT_SUITE == (
        "baseline-camera", "baseline-animation-camera",
        "backpack-side-camera", "audio-idle", "audio-camera")


def test_terminal_report_contains_actual_timeline_and_recent_wire_commands(tmp_path, capsys):
    import json
    entries = [
        {"utc": "2026-11-22T17:05:13+00:00", "event": "command_issued",
         "action": "stock audio", "transport": {
            "next_ack_expected": 131, "pending_reliable_packets": 5,
            "queued_app_commands": 0, "outgoing_packets": 142,
            "received_frames": 140, "audio_frames_reported_played": 55}},
        {"utc": "2026-11-22T17:05:14+00:00", "event": "sample",
         "transport": {"next_ack_expected": 136, "pending_reliable_packets": 5,
                       "queued_app_commands": 2, "outgoing_packets": 150,
                       "sent_packets_including_retries": 165,
                       "received_frames": 180, "received_packets": 700,
                       "audio_frames_reported_played": 60}},
        {"utc": "2026-11-22T17:05:18+00:00", "event": "stalled",
         "transport": {"next_ack_expected": 136, "pending_reliable_packets": 62,
                       "queued_app_commands": 202, "outgoing_packets": 206,
                       "sent_packets_including_retries": 206,
                       "received_frames": 333, "received_packets": 1035,
                       "audio_frames_reported_played": 62}},
        {"utc": "2026-11-22T17:05:18+00:00", "event": "sample",
         "transport": {"next_ack_expected": 136, "pending_reliable_packets": 62,
                       "queued_app_commands": 202, "outgoing_packets": 206,
                       "sent_packets_including_retries": 206,
                       "received_frames": 333, "received_packets": 1035,
                       "audio_frames_reported_played": 62}},
    ]
    (tmp_path / "measurements.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in entries))
    (tmp_path / "incidents.jsonl").write_text(json.dumps({
        "event": "disconnection_snapshot", "recent_protocol_packets": [
            {"direction": "tx", "packet": "OutputAudio",
             "bytes": 744, "id": 15, "utc": "2026-11-22T17:05:18Z"}]}) + "\n")
    result = hardware_tests._summarize(tmp_path, 3)
    hardware_tests._print_summary("audio-camera", result, tmp_path)
    report = capsys.readouterr().out
    assert "ACK=  136" in report
    assert "pending= 62/62" in report
    assert "queue=  202" in report
    assert "OutputAudio" in report
    assert "audio=  62" in report
    assert "Last 20 queued-to-send packet types" in report
