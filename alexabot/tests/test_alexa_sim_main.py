"""``python -m alexabot.sim``: flags, and the refusals that name every
problem at once. Offline; nothing here starts Bedrock or Polly."""

import builtins
import socket

import pytest

from alexabot import sim


def test_scripted_is_shorthand_for_three_flags():
    config = sim.load_sim_config(["--scripted"], env={})
    assert (config.agent, config.workers, config.tts) == ("scripted", "scripted",
                                                          "browser")
    assert config.mode == "scripted"
    assert config.scripted_delay_s == sim.SCRIPTED_SIM_DELAY_S
    live = sim.load_sim_config([], env={"ALEXA_SIM_MODEL_ID": "m", "AWS_REGION": "r"})
    assert (live.agent, live.workers, live.tts) == ("bedrock", "live", "polly")
    assert (live.model_id, live.region, live.scripted_delay_s) == ("m", "r", 0.0)


def test_scripted_banner_and_config(tmp_path, monkeypatch, loopback_only):
    pytest.importorskip("strands")
    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    config = sim.load_sim_config(["--scripted", "--port", "0", "--mcp-port", "0",
                                  "--db", str(tmp_path / "b.sqlite3")], env={})
    built = sim.build(config, env={})
    try:
        words = sim.banner(built)
        assert words.startswith("Simulated Alexa+ experience at http://127.0.0.1:")
        assert "SCRIPTED" in words and "it is not Alexa" in words
        view = built.app.config_view()
        assert view["mode"] == "scripted" and view["agent"]["kind"] == "scripted"
        assert view["tts"] == {"kind": "browser"}
        assert view["mcp"]["spec"] == "2025-11-25"
        assert sorted(view["mcp"]["tools"]) == sorted(sim.ADA_TOOLS)
        assert view["board_images"] is True
    finally:
        built.close()


def test_bedrock_without_credentials_refuses_naming_scripted(monkeypatch):
    boto3 = pytest.importorskip("boto3")

    class NoCredentials:
        def get_credentials(self):
            return None

    monkeypatch.setattr(boto3, "Session", NoCredentials)
    config = sim.load_sim_config(["--agent", "bedrock", "--workers", "scripted",
                                  "--mcp-port", "0", "--port", "0"], env={})
    problems = sim.preflight(config)
    assert any("no AWS credentials" in p and "--scripted" in p for p in problems)


def test_missing_strands_refuses_naming_the_install_line(monkeypatch):
    real = builtins.__import__

    def refuse(name, *args, **kwargs):
        if name == "strands" or name.startswith("strands."):
            raise ImportError("no strands here")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse)
    config = sim.load_sim_config(["--scripted", "--mcp-port", "0", "--port", "0"],
                                 env={})
    problems = sim.preflight(config)
    assert any(sim.INSTALL_LINE in p for p in problems)


def test_non_loopback_host_refuses(capsys):
    with pytest.raises(sim.SimConfigError) as caught:
        sim.load_sim_config(["--scripted", "--host", "0.0.0.0"], env={})
    assert "not loopback" in str(caught.value)
    assert sim.main(["--scripted", "--host", "0.0.0.0"]) == 2
    assert "not loopback" in capsys.readouterr().err


def test_busy_mcp_port_refuses_naming_mcp_port_0():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen(1)
    port = holder.getsockname()[1]
    try:
        config = sim.load_sim_config(["--scripted", "--mcp-port", str(port),
                                      "--port", "0"], env={})
        problems = sim.preflight(config)
        assert any(f"port {port} is busy" in p and "--mcp-port 0" in p
                   for p in problems)
    finally:
        holder.close()


def test_every_problem_is_named_at_once():
    with pytest.raises(sim.SimConfigError) as caught:
        sim.load_sim_config(["--port", "x", "--max-model-calls", "-1",
                             "--host", "10.0.0.1", "--mcp-url", "ftp://nope"], env={})
    assert len(caught.value.errors) == 4


def test_mcp_url_turns_board_images_off_and_says_so(tmp_path, monkeypatch,
                                                    loopback_only):
    pytest.importorskip("strands")
    from alexabot import app as alexa_app
    from alexabot.config import Config

    monkeypatch.setenv("SILKSCREEN_STEPS_DIR", str(tmp_path / "steps"))
    alexa = Config(port=0, db=str(tmp_path / "remote.sqlite3"), scripted=True)
    runner = alexa_app.build_runner(alexa)
    remote = alexa_app.make_server(alexa, runner=runner)
    import threading

    threading.Thread(target=remote.serve_forever, args=(0.05,), daemon=True).start()
    url = f"http://127.0.0.1:{remote.server_address[1]}/mcp"
    config = sim.load_sim_config(["--scripted", "--port", "0", "--mcp-url", url],
                                 env={})
    built = sim.build(config, env={})
    try:
        assert built.mcp_server is None and built.app.board_file is None
        assert built.app.config_view()["board_images"] is False
        assert built.factory.images is False
    finally:
        built.close()
        remote.shutdown()
        remote.server_close()
        runner.join(5)
        runner.store.close()
