"""External Home Assistant speech must not become progress in a retained music queue."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from music_assistant_models.enums import MediaType, PlaybackState, PlayerType
from music_assistant_models.media_items import AudioFormat, Track
from music_assistant_models.player import PlayerMedia
from music_assistant_models.player_queue import PlayerQueue, PlayLogEntry
from music_assistant_models.queue_item import QueueItem
from music_assistant_models.streamdetails import StreamDetails

from music_assistant.constants import ATTR_ANNOUNCEMENT_IN_PROGRESS
from music_assistant.controllers.player_queues import PlayerQueuesController
from music_assistant.controllers.player_queues.state import PlayerQueueData
from music_assistant.controllers.players import PlayerController
from music_assistant.helpers.player import is_external_announcement
from tests.common import MockPlayer, MockProvider


@pytest.fixture
def flow() -> tuple[Any, MockPlayer, MockPlayer, PlayerQueueData]:
    """Build real controllers and a linked output with music on the third flow item."""
    mass = MagicMock()
    mass.closing = False
    mass.config.get_raw_core_config_value.side_effect = lambda _id, _key, default=None: default
    mass.config.get_raw_player_config_value.side_effect = lambda _id, _key, default=None: default
    mass.streams.base_url = "http://ma:8097"
    mass.streams.is_smart_fades_active.return_value = False
    mass.streams.audio_processing.retain_outputs.return_value = False
    mass.players = PlayerController(mass)
    mass.player_queues = PlayerQueuesController(mass)
    provider = MockProvider("test", mass=mass)
    mass.get_provider.return_value = provider
    parent = MockPlayer(provider, "queue", "Bathroom")
    output = MockPlayer(provider, "cast", "Chromecast", player_type=PlayerType.PROTOCOL)
    mass.players._players.update({"queue": parent, "cast": output})
    items = []
    for item_id, duration in [("previous", 163), ("middle", 98), ("current", 170)]:
        track = Track(
            item_id=item_id,
            provider="test",
            name=item_id,
            duration=duration,
            provider_mappings=set(),
        )
        item = QueueItem.from_media_item("queue", track)
        item.queue_item_id = item_id
        item.streamdetails = StreamDetails(
            provider="test", item_id=item_id, audio_format=AudioFormat()
        )
        items.append(item)
    assert items[0].streamdetails is not None
    items[0].streamdetails.seek_position = 82
    queue = PlayerQueue(
        queue_id="queue",
        active=True,
        display_name="Bathroom",
        available=True,
        items=3,
        state=PlaybackState.PLAYING,
        flow_mode=True,
        current_index=2,
        current_item=items[2],
        next_item=None,
        elapsed_time=146,
        elapsed_time_last_updated=time.time(),
    )
    data = PlayerQueueData(
        queue=queue,
        items=items,
        session_id="session",
        flow_mode_stream_log=[
            PlayLogEntry(queue_item_id="previous", seconds_streamed=81),
            PlayLogEntry(queue_item_id="middle", seconds_streamed=98),
            PlayLogEntry(queue_item_id="current"),
        ],
    )
    mass.player_queues._queue_data["queue"] = data
    output.set_current_media(uri="http://ma:8097/flow/session/queue/previous/cast.flac")
    output._attr_playback_state = PlaybackState.PLAYING
    output._attr_elapsed_time = 325
    output._attr_elapsed_time_last_updated = time.time()
    output.refresh_state(signal_event=False)
    parent.set_active_output_protocol("cast")
    parent.refresh_state(signal_event=False)
    return mass, parent, output, data


def _report(
    parent: MockPlayer, output: MockPlayer, state: PlaybackState, uri: str | None, elapsed: float
) -> None:
    """Apply a physical device report, including a new raw audio identity."""
    output._attr_current_media = PlayerMedia(uri=uri, media_type=MediaType.TRACK) if uri else None
    output._attr_playback_state = state
    output._attr_elapsed_time = elapsed
    output._attr_elapsed_time_last_updated = time.time()
    output.refresh_state(signal_event=False)
    parent.refresh_state(signal_event=False)


@pytest.mark.parametrize(
    "uri",
    [
        "http://ha:8123/api/tts_proxy/speech.mp3",
        "https://external-ha/api/tts_proxy/speech.wav?authSig=new",
        "/api/tts_proxy/speech.ogg",
    ],
)
@pytest.mark.parametrize("late_music_report", [False, True])
async def test_tts_cannot_change_saved_flow_item_or_position(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], uri: str, late_music_report: bool
) -> None:
    """Speech's PLAYING/IDLE reports and timing corrections preserve the paused music pair."""
    mass, parent, output, data = flow
    ctrl = mass.player_queues
    with patch.object(mass.players, "_handle_cmd_pause", new=AsyncMock()):
        await ctrl.pause("queue")
    position = data.queue.resume_pos
    _report(
        parent,
        output,
        PlaybackState.PAUSED,
        "http://ma:8097/flow/session/queue/previous/cast.flac",
        325,
    )
    ctrl.on_player_update(parent, {})
    assert data.pause_confirmed
    if late_music_report:
        _report(
            parent,
            output,
            PlaybackState.PLAYING,
            "http://ma:8097/flow/session/queue/previous/cast.flac",
            325,
        )
        ctrl.on_player_update(parent, {})
        assert not data.pause_update_hold
    saved_elapsed = data.queue.elapsed_time
    for state, elapsed in [
        (PlaybackState.PLAYING, 5),
        (PlaybackState.PLAYING, 35),
        (PlaybackState.IDLE, 40),
    ]:
        _report(parent, output, state, uri, elapsed)
        assert parent.state.active_source == "queue"
        # The final metadata still names music, so testing it would miss the takeover.
        assert parent.state.current_media is not None
        assert parent.state.current_media.queue_item_id == "current"
        ctrl.on_player_update(parent, {})
        ctrl.on_player_elapsed_time_corrected(parent)
        assert data.queue.current_index == 2
        assert data.queue.current_item == data.items[2]
        assert data.queue.elapsed_time == saved_elapsed
        assert data.queue.resume_pos == position
        assert data.queue.state == PlaybackState.PAUSED
        assert data.pause_update_hold

    with patch.object(ctrl, "play_index", new=AsyncMock()) as play_index:
        await mass.players.cmd_play("queue")
    play_index.assert_awaited_once_with("queue", "current", position, False)


@pytest.mark.parametrize("command", ["player", "internal", "queue"])
@pytest.mark.parametrize("state", [PlaybackState.PLAYING, PlaybackState.PAUSED, PlaybackState.IDLE])
async def test_resume_after_external_tts_restarts_music_instead_of_ignoring_or_unpausing_speech(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], command: str, state: PlaybackState
) -> None:
    """All play entrypoints resume the retained queue even when the output still reports speech."""
    mass, parent, output, data = flow
    data.queue.state = PlaybackState.PAUSED
    data.queue.resume_pos = 146
    data.transitioning = data.pause_update_hold = data.pause_confirmed = True
    _report(parent, output, state, "http://ha/api/tts_proxy/finished.mp3", 12)
    with patch.object(mass.player_queues, "play_index", new=AsyncMock()) as play_index:
        if command == "player":
            await mass.players.cmd_play("queue")
        elif command == "internal":
            await mass.players._handle_cmd_play("queue")
        else:
            await mass.player_queues.play("queue")
    play_index.assert_awaited_once_with("queue", "current", 146, False)


async def test_cleared_speech_with_stale_playing_does_not_release_pause_hold(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData],
) -> None:
    """Missing raw media is not proof that the music stream resumed."""
    mass, parent, output, data = flow
    data.queue.state = PlaybackState.PAUSED
    data.queue.resume_pos = 146
    data.transitioning = data.pause_update_hold = data.pause_confirmed = True
    saved_elapsed = data.queue.elapsed_time
    _report(parent, output, PlaybackState.PLAYING, None, 12)
    mass.player_queues.on_player_update(parent, {})
    mass.player_queues.on_player_elapsed_time_corrected(parent)
    assert data.pause_update_hold
    assert data.queue.current_index == 2
    assert data.queue.elapsed_time == saved_elapsed
    with patch.object(mass.player_queues, "play_index", new=AsyncMock()) as play_index:
        await mass.players.cmd_play("queue")
    play_index.assert_awaited_once_with("queue", "current", 146, False)


def test_real_music_resume_releases_hold_and_tracks_later_flow_items(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData],
) -> None:
    """The fix must not recreate the original freeze after a real music unpause."""
    mass, parent, output, data = flow
    data.queue.state = PlaybackState.PAUSED
    data.transitioning = data.pause_update_hold = data.pause_confirmed = True
    _report(
        parent,
        output,
        PlaybackState.PLAYING,
        "http://ma:8097/flow/session/queue/previous/cast.flac",
        330,
    )
    mass.player_queues.on_player_update(parent, {})
    assert not data.transitioning
    assert not data.pause_update_hold
    assert data.queue.current_index == 2
    assert data.queue.elapsed_time == pytest.approx(151, abs=1)


@pytest.mark.parametrize(
    "uri", ["http://ma:8097/flow/session/queue/previous/cast.flac", "http://radio/music.mp3", None]
)
def test_ordinary_audio_is_not_classified_as_external_speech(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], uri: str | None
) -> None:
    """An extension alone, or missing metadata, does not identify announcement audio."""
    _, parent, output, _ = flow
    _report(parent, output, PlaybackState.PLAYING, uri, 5)
    assert not is_external_announcement(parent)


@pytest.mark.parametrize("guard", ["transition", "ma-announcement"])
def test_position_correction_respects_existing_update_holds(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], guard: str
) -> None:
    """The discrete-position correction path cannot bypass a transition or announcement hold."""
    mass, parent, output, data = flow
    if guard == "transition":
        data.transitioning = True
    else:
        parent.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = True
    position = data.queue.elapsed_time
    _report(
        parent,
        output,
        PlaybackState.PLAYING,
        "http://ma:8097/flow/session/queue/previous/cast.flac",
        5,
    )
    mass.player_queues.on_player_elapsed_time_corrected(parent)
    assert data.queue.elapsed_time == position
    assert data.queue.current_index == 2


async def test_external_audio_does_not_start_an_inactive_retained_queue(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData],
) -> None:
    """An unrelated output must not cause an inactive music queue to take it over."""
    mass, parent, output, data = flow
    data.queue.active = False
    data.queue.state = PlaybackState.PAUSED
    _report(parent, output, PlaybackState.PLAYING, "http://ha/api/tts_proxy/speech.mp3", 12)
    with patch.object(mass.player_queues, "play_index", new=AsyncMock()) as play_index:
        await mass.players.cmd_play("queue")
    play_index.assert_not_awaited()


@pytest.mark.parametrize("command", ["player", "internal", "queue"])
async def test_delayed_old_music_reports_after_tts_do_not_confirm_resume(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], command: str
) -> None:
    """Keep the interruption hold through stale music reports and cleared speech metadata."""
    mass, parent, output, data = flow
    ctrl = mass.player_queues
    with patch.object(mass.players, "_handle_cmd_pause", new=AsyncMock()):
        await ctrl.pause("queue")
    _report(parent, output, PlaybackState.PLAYING, "http://ha/api/tts_proxy/speech.mp3", 5)
    ctrl.on_player_update(parent, {})
    saved_elapsed = data.queue.elapsed_time
    for state, uri, elapsed in [
        (PlaybackState.PLAYING, "http://ma:8097/flow/session/queue/previous/cast.flac", 325),
        (PlaybackState.IDLE, None, 0),
        (PlaybackState.PLAYING, None, 40),
        (PlaybackState.PAUSED, None, 40),
    ]:
        _report(parent, output, state, uri, elapsed)
        ctrl.on_player_update(parent, {})
        ctrl.on_player_elapsed_time_corrected(parent)
        assert data.external_announcement_hold
        assert data.pause_update_hold
        assert data.queue.state == PlaybackState.PAUSED
        assert data.queue.current_index == 2
        assert data.queue.elapsed_time == saved_elapsed
        assert data.queue.resume_pos == 146
    with patch.object(ctrl, "play_index", new=AsyncMock()) as play_index:
        if command == "player":
            await mass.players.cmd_play("queue")
        elif command == "internal":
            await mass.players._handle_cmd_play("queue")
        else:
            await ctrl.play("queue")
    play_index.assert_awaited_once_with("queue", "current", 146, False)


def test_old_flow_session_reports_cannot_rewrite_resumed_queue_progress(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData],
) -> None:
    """Position updates belong to the actual active flow session, not just its queue id."""
    mass, parent, output, data = flow
    data.session_id = "resumed"
    saved_elapsed = data.queue.elapsed_time
    _report(
        parent,
        output,
        PlaybackState.PLAYING,
        "http://ma:8097/flow/session/queue/previous/cast.flac",
        5,
    )
    mass.player_queues.on_player_update(parent, {})
    mass.player_queues.on_player_elapsed_time_corrected(parent)
    assert data.queue.current_index == 2
    assert data.queue.elapsed_time == saved_elapsed
    _report(
        parent,
        output,
        PlaybackState.PLAYING,
        "http://ma:8097/flow/resumed/queue/previous/cast.flac",
        330,
    )
    mass.player_queues.on_player_update(parent, {})
    assert data.queue.current_index == 2
    assert data.queue.elapsed_time == pytest.approx(151, abs=1)


@pytest.mark.parametrize("native", [False, True])
def test_typed_announcement_distinguishes_external_from_ma_owned_audio(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], native: bool
) -> None:
    """MA's existing announcement lifecycle remains responsible for its own audio."""
    _, parent, output, _ = flow
    output._attr_current_media = PlayerMedia(
        uri="http://speech.example/announcement.ogg", media_type=MediaType.ANNOUNCEMENT
    )
    output.extra_data[ATTR_ANNOUNCEMENT_IN_PROGRESS] = native
    output.refresh_state(signal_event=False)
    parent.refresh_state(signal_event=False)
    assert is_external_announcement(parent) is not native


async def test_successive_announcements_retain_each_new_music_resume_point(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData],
) -> None:
    """A later interruption takes its own snapshot, not the first speech's flow position."""
    mass, parent, output, data = flow
    ctrl = mass.player_queues
    with patch.object(mass.players, "_handle_cmd_pause", new=AsyncMock()):
        await ctrl.pause("queue")
    _report(parent, output, PlaybackState.PLAYING, "http://ha/api/tts_proxy/first.mp3", 5)
    ctrl.on_player_update(parent, {})
    ctrl._set_transitioning("queue", True)
    data.session_id = "resumed"
    data.queue.resume_pos = 0
    data.queue.state = PlaybackState.PLAYING
    ctrl._set_transitioning("queue", False)
    _report(
        parent,
        output,
        PlaybackState.PLAYING,
        "http://ma:8097/flow/resumed/queue/previous/cast.flac",
        330,
    )
    ctrl.on_player_update(parent, {})
    assert not ctrl.has_external_announcement_hold("queue")
    with patch.object(mass.players, "_handle_cmd_pause", new=AsyncMock()):
        await ctrl.pause("queue")
    position = data.queue.resume_pos
    assert position == 151
    _report(parent, output, PlaybackState.PLAYING, "http://ha/api/tts_proxy/second.mp3", 60)
    ctrl.on_player_update(parent, {})
    ctrl.on_player_elapsed_time_corrected(parent)
    assert data.interrupted_flow_session_id == "resumed"
    _report(parent, output, PlaybackState.IDLE, None, 60)
    ctrl.on_player_update(parent, {})
    assert data.queue.current_index == 2
    assert data.queue.resume_pos == position
    with patch.object(ctrl, "play_index", new=AsyncMock()) as play_index:
        await mass.players.cmd_play("queue")
    play_index.assert_awaited_once_with("queue", "current", position, False)


@pytest.mark.parametrize("late_speech_report", [False, True])
def test_external_resume_waits_for_new_stream_identity_before_accepting_unknown_progress(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData], late_speech_report: bool
) -> None:
    """The gap between sending resumed music and seeing its renderer is still protected."""
    mass, parent, output, data = flow
    ctrl = mass.player_queues
    _report(parent, output, PlaybackState.PLAYING, "http://ha/api/tts_proxy/speech.mp3", 5)
    ctrl.on_player_update(parent, {})
    assert data.interrupted_flow_session_id == "session"
    saved_elapsed = data.queue.elapsed_time
    # Model the track-load boundary: the command starts a new session, but its
    # physical renderer reports arrive later. All reconciliation below is real.
    ctrl._set_transitioning("queue", True)
    data.session_id = "resumed"
    data.queue.state = PlaybackState.PLAYING
    ctrl._set_transitioning("queue", False)
    assert ctrl.has_external_announcement_hold("queue")
    if late_speech_report:
        ctrl.on_player_update(parent, {})
    for uri, elapsed in [(None, 40), ("http://ma:8097/flow/session/queue/previous/cast.flac", 325)]:
        _report(parent, output, PlaybackState.PLAYING, uri, elapsed)
        ctrl.on_player_update(parent, {})
        ctrl.on_player_elapsed_time_corrected(parent)
        assert data.queue.current_index == 2
        assert data.queue.elapsed_time == saved_elapsed
        assert ctrl.has_external_announcement_hold("queue")
    _report(
        parent,
        output,
        PlaybackState.PLAYING,
        "http://ma:8097/flow/resumed/queue/previous/cast.flac",
        330,
    )
    ctrl.on_player_update(parent, {})
    assert not ctrl.has_external_announcement_hold("queue")
    assert data.queue.state == PlaybackState.PLAYING
    assert data.queue.current_index == 2
    assert data.queue.elapsed_time == pytest.approx(151, abs=1)


async def test_repeated_resume_before_music_confirmation_uses_frozen_position_not_clock(
    flow: tuple[Any, MockPlayer, MockPlayer, PlayerQueueData],
) -> None:
    """Starting a replacement stream is not yet evidence that music time is advancing."""
    mass, parent, output, data = flow
    ctrl = mass.player_queues
    _report(parent, output, PlaybackState.PLAYING, "http://ha/api/tts_proxy/speech.mp3", 5)
    ctrl.on_player_update(parent, {})
    ctrl._set_transitioning("queue", True)
    data.session_id = "resumed"
    data.queue.resume_pos = 0
    data.queue.state = PlaybackState.PLAYING
    ctrl._set_transitioning("queue", False)
    data.queue.elapsed_time_last_updated -= 90
    assert data.queue.corrected_elapsed_time > 200
    with patch.object(ctrl, "play_index", new=AsyncMock()) as play_index:
        await ctrl.resume("queue")
    play_index.assert_awaited_once_with("queue", "current", 146, False)
