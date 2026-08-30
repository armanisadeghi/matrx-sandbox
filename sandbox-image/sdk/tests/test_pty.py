import signal
from unittest.mock import patch

from matrx_agent.api.pty import _write_client_text


def test_json_scalar_keystroke_is_written_to_pty() -> None:
    with patch("matrx_agent.api.pty.os.write") as write:
        _write_client_text(7, 42, "2")

    write.assert_called_once_with(7, b"2")


def test_resize_control_frame_is_not_written_as_input() -> None:
    with (
        patch("matrx_agent.api.pty.os.write") as write,
        patch("matrx_agent.api.pty.set_winsize") as resize,
    ):
        _write_client_text(7, 42, '{"type":"resize","cols":90,"rows":24}')

    resize.assert_called_once_with(7, 24, 90)
    write.assert_not_called()


def test_signal_control_frame_targets_the_foreground_process_group() -> None:
    with (
        patch("matrx_agent.api.pty.os.write") as write,
        patch("matrx_agent.api.pty.os.tcgetpgrp", return_value=314) as foreground,
        patch("matrx_agent.api.pty.os.killpg") as kill_group,
        patch("matrx_agent.api.pty.os.kill") as kill_process,
    ):
        _write_client_text(7, 42, '{"type":"signal","name":"SIGINT"}')

    foreground.assert_called_once_with(7)
    kill_group.assert_called_once_with(314, signal.SIGINT)
    kill_process.assert_not_called()
    write.assert_not_called()
