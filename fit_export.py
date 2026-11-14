"""Write a FIT activity using the same WGS-84 route as the emulator."""

from datetime import datetime, timezone
from math import ceil, floor
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from campus import CampusTrack, RunPlan


def export_fit(path: Path, track: CampusTrack, plan: RunPlan, end: datetime | None = None) -> None:
    try:
        from fit_tool.definition_message import DefinitionMessage
        from fit_tool.fit_file_header import FitFileHeader
        from fit_tool.profile.messages.activity_message import ActivityMessage
        from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
        from fit_tool.profile.messages.event_message import EventMessage
        from fit_tool.profile.messages.file_id_message import FileIdMessage
        from fit_tool.profile.messages.lap_message import LapMessage
        from fit_tool.profile.messages.record_message import RecordMessage
        from fit_tool.profile.messages.session_message import SessionMessage
        from fit_tool.profile.messages.sport_message import SportMessage
        from fit_tool.profile.profile_type import Activity, ActivityType, Event, EventType, FileType, Manufacturer, Sport, SubSport
        from fit_tool.record import Record
        from fit_tool.utils.conversions import to_seconds_since_1989_epoch
        from fit_tool.utils.crc import crc16_chunks
    except ImportError as error:
        raise RuntimeError("源码版导出 FIT 需要运行 py -3 -m pip install -r requirements.txt。") from error

    # fit-tool takes Unix milliseconds. FIT record timestamps resolve to whole seconds.
    finish = int((end or datetime.now(timezone.utc)).timestamp()) * 1000
    duration = max(1, round(plan.duration))
    start = finish - duration * 1000
    clock_scale = plan.duration / duration
    path_line = track.travel_path(plan)
    pacing = plan.pacing
    body = bytearray()

    def append_message(message):
        definition = DefinitionMessage.from_data_message(message)
        body.extend(Record.from_message(definition).to_bytes())
        message.set_definition_message(definition)
        record = Record.from_message(message)
        body.extend(record.to_bytes())
        return record

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 1
    file_id.product_name = "Keep EDA Simulator"
    file_id.serial_number = 0x45444101
    file_id.time_created = start
    append_message(file_id)

    device = DeviceInfoMessage()
    device.timestamp = start
    device.device_index = 0
    device.manufacturer = Manufacturer.DEVELOPMENT.value
    device.product = file_id.product
    device.serial_number = file_id.serial_number
    device.software_version = 3.0
    append_message(device)
    sport = SportMessage()
    sport.sport = Sport.RUNNING
    sport.sub_sport = SubSport.GENERIC
    append_message(sport)

    timer = EventMessage()
    timer.timestamp = start
    timer.event = Event.TIMER
    timer.event_type = EventType.START
    append_message(timer)

    sample = RecordMessage()
    sample.activity_type = ActivityType.RUNNING
    wire_sample = None
    for second in range(duration + 1):
        motion = pacing.at(plan.duration * (second / duration))
        sample.position_long, sample.position_lat = path_line.at(motion.meters)
        sample.distance = motion.meters
        sample.speed = motion.speed * clock_scale
        # FIT running cadence and total_cycles count full strides (two footfalls).
        rpm = motion.cadence * clock_scale / 2
        sample.cadence = floor(rpm)
        sample.fractional_cadence = floor((rpm % 1)*128)/128
        sample.total_cycles = motion.steps // 2
        sample.step_length = motion.step_length * 1000
        sample.timestamp = start + second * 1000
        if wire_sample is None:
            wire_sample = append_message(sample)
        else:
            body.extend(wire_sample.to_bytes())

    timer.timestamp = finish
    timer.event_type = EventType.STOP_ALL
    append_message(timer)

    def summarize(message, d0: float, d1: float, start_s: int, end_s: int, index: int):
        t0, t1 = pacing.time_at(d0), pacing.time_at(d1)
        first, last = pacing.at(t0), pacing.at(t1)
        segment_seconds = end_s-start_s
        steps = last.steps - first.steps
        rpm = steps*30/segment_seconds
        bounds = range(floor(t0/pacing.interval)+1, min(pacing.count, ceil(t1/pacing.interval)))
        peak_speed = max([first.speed, last.speed] + [pacing.speeds[i] for i in bounds])
        peak_rpm = max([first.cadence, last.cadence] + [pacing.cadences[i] for i in bounds])*clock_scale/2
        message.message_index = index
        message.start_time = start + start_s*1000
        message.timestamp = start + end_s*1000
        message.sport = Sport.RUNNING
        message.sub_sport = SubSport.GENERIC
        message.total_timer_time = message.total_elapsed_time = segment_seconds
        message.total_distance = d1-d0
        message.avg_speed = (d1-d0)/segment_seconds
        message.max_speed = max(message.avg_speed, peak_speed * clock_scale)
        message.avg_cadence = floor(rpm)
        message.avg_fractional_cadence = floor((rpm % 1)*128)/128
        peak_rpm = max(peak_rpm, rpm)
        message.max_cadence = floor(peak_rpm)
        message.max_fractional_cadence = floor((peak_rpm % 1)*128)/128
        message.total_cycles = steps // 2
        message.total_fractional_cycles = (steps % 2)/2
        message.avg_step_length = 1000*(d1-d0)/steps if steps else 0
        message.start_position_long, message.start_position_lat = path_line.at(d0)
        append_message(message)

    # Keep whole-second lap timestamps strictly increasing, including a tiny final partial km.
    splits = [(0, 0)]
    for meters in range(1000, plan.meters, 1000):
        t = round(pacing.time_at(meters)/plan.duration*duration)
        if splits[-1][1] < t < duration:
            splits.append((meters, t))
    splits.append((plan.meters, duration))
    for i, ((d0, t0), (d1, t1)) in enumerate(zip(splits, splits[1:])):
        summarize(LapMessage(), d0, d1, t0, t1, i)
    session = SessionMessage()
    session.num_laps = len(splits)-1
    session.first_lap_index = 0
    summarize(session, 0, plan.meters, 0, duration, 0)
    activity = ActivityMessage()
    activity.timestamp = finish
    if end is not None and end.utcoffset() is not None:
        # The FIT local_date_time field stores wall-clock seconds from the FIT epoch.
        activity.local_timestamp = to_seconds_since_1989_epoch(finish) + int(end.utcoffset().total_seconds())
    activity.total_timer_time = duration
    activity.num_sessions = 1
    activity.type = Activity.MANUAL
    activity.event = Event.ACTIVITY
    activity.event_type = EventType.STOP
    append_message(activity)

    header = FitFileHeader(records_size=len(body), gen_crc=True).to_bytes()
    crc = crc16_chunks((header, body)).to_bytes(2, "little")
    temporary_path = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as temp:
            temporary_path = Path(temp.name)
            temp.writelines((header, body, crc))
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
