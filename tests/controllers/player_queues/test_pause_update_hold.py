"""Pause must not freeze the queue's current item after playback resumes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock

from music_assistant_models.enums import PlaybackState, PlayerType
from music_assistant_models.player_queue import PlayerQueue

from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData


def _controller() -> Any:
    """Build a queue controller with one active playing queue."""
    ctrl = PlayerQueuesController.__new__(PlayerQueuesController)
    ctrl.logger = Mock()
    ctrl.mass = Mock()
    ctrl.mass.cancel_timer = Mock()
    ctrl.mass.create_task = Mock()
    ctrl.mass.players._handle_cmd_pause = AsyncMock()
    ctrl.mass.players.get_player = Mock(return_value=None)
    ctrl._check_player_permission = Mock()
    ctrl._update_queue_from_player = Mock()
    queue = PlayerQueue(
        queue_id="q1",
        active=True,
        display_name="Q1",
        available=True,
        items=0,
        state=PlaybackState.PLAYING,
    )
    ctrl._queue_data = {"q1": PlayerQueueData(queue=queue)}
    return ctrl


def _player(state: PlaybackState) -> Mock:
    """Build a player report for queue q1."""
    player = Mock()
    player.type = PlayerType.PLAYER
    player.player_id = "q1"
    player.extra_data = {}
    player.state.playback_state = state
    player.state.active_source = "q1"
    return player


async def test_pause_ignores_stale_playing_until_resume_is_confirmed() -> None:
    """A flow keeps its queue index moving after pause/resume outside queue.play()."""
    ctrl = _controller()

    await ctrl.pause("q1")

    queue_data = ctrl._queue_data["q1"]
    assert queue_data.transitioning
    assert queue_data.pause_update_hold
    assert queue_data.queue.state == PlaybackState.PAUSED

    playing = _player(PlaybackState.PLAYING)
    ctrl.on_player_update(playing, {})
    ctrl._update_queue_from_player.assert_not_called()

    ctrl.on_player_update(_player(PlaybackState.PAUSED), {})
    ctrl._update_queue_from_player.assert_not_called()
    assert queue_data.pause_confirmed

    ctrl.on_player_update(playing, {})
    ctrl._update_queue_from_player.assert_called_once_with(playing)
    assert not queue_data.transitioning
    assert not queue_data.pause_update_hold

    ctrl.on_player_update(playing, {})
    assert ctrl._update_queue_from_player.call_count == 2


def test_track_transition_still_ignores_player_reports() -> None:
    """Loading the next track keeps ignoring player reports. That is not a pause hold."""
    ctrl = _controller()
    ctrl._set_transitioning("q1", True)

    ctrl.on_player_update(_player(PlaybackState.PLAYING), {})
    ctrl.on_player_update(_player(PlaybackState.PAUSED), {})
    ctrl.on_player_update(_player(PlaybackState.PLAYING), {})

    ctrl._update_queue_from_player.assert_not_called()
    assert ctrl._queue_data["q1"].transitioning


def test_releasing_a_confirmed_pause_does_not_cancel_a_track_transition() -> None:
    """An explicit play only releases the pause hold, not a track load."""
    ctrl = _controller()
    ctrl._set_transitioning("q1", True)

    ctrl.release_confirmed_pause_hold("q1")
    ctrl.release_pause_update_hold("q1")

    assert ctrl._queue_data["q1"].transitioning
