from types import SimpleNamespace

from app.pipeline.alerts import AlertTracker

SOS = "person_requesting_help_sos"
DETECTOR = SimpleNamespace(id="d1", name="personas", alert_min_confidence=0.7, alert_min_frames=3)


def det(alert=SOS, conf=0.9, track=1, offset=0.0):
    return SimpleNamespace(alert=alert, confidence=conf, track_id=track, offset_s=offset, alert_confirmed=False)


def run(frames, detector=DETECTOR):
    """frames: lista de (indice_de_frame, [detecciones]); devuelve las confirmaciones por frame."""
    tracker = AlertTracker()
    return [tracker.observe(i, detector, dets) for i, dets in frames]


def test_alert_is_confirmed_on_the_nth_consecutive_frame_and_marks_the_whole_streak():
    dets = [det(offset=float(i)) for i in range(3)]
    out = run([(i, [dets[i]]) for i in range(3)])

    assert [len(c) for c in out] == [0, 0, 1]
    c = out[2][0]
    assert (c.alert, c.track_id, c.frames, c.first_offset_s, c.offset_s) == (SOS, 1, 3, 0.0, 2.0)
    assert all(d.alert_confirmed for d in dets)


def test_only_one_confirmation_per_streak_but_later_frames_are_marked():
    dets = [det(offset=float(i)) for i in range(5)]
    out = run([(i, [dets[i]]) for i in range(5)])

    assert sum(len(c) for c in out) == 1
    assert all(d.alert_confirmed for d in dets)


def test_a_missing_frame_breaks_the_streak():
    dets = [det(), det(), det()]
    out = run([(0, [dets[0]]), (1, [dets[1]]), (3, [dets[2]])])  # el frame 2 no trajo la alerta

    assert not any(out)
    assert not any(d.alert_confirmed for d in dets)


def test_low_confidence_does_not_count_and_breaks_the_streak():
    dets = [det(conf=0.9), det(conf=0.5), det(conf=0.9), det(conf=0.9)]
    out = run([(i, [d]) for i, d in enumerate(dets)])

    assert not any(out)


def test_different_people_do_not_add_up():
    out = run([(0, [det(track=1)]), (1, [det(track=2)]), (2, [det(track=3)])])

    assert not any(out)


def test_frames_without_alert_are_ignored():
    assert not any(run([(i, [det(alert=None)]) for i in range(5)]))


def test_each_detector_uses_its_own_thresholds():
    strict = SimpleNamespace(id="d2", name="estricto", alert_min_confidence=0.95, alert_min_frames=2)
    lax = SimpleNamespace(id="d3", name="laxo", alert_min_confidence=0.5, alert_min_frames=1)

    assert not any(run([(0, [det(conf=0.9)]), (1, [det(conf=0.9)])], strict))
    assert len(run([(0, [det(conf=0.6)])], lax)[0]) == 1
