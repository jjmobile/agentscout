from datetime import timedelta

from agentscout.main import Runner
from conftest import DID_A, NOW


class Notes:
    def __init__(self):
        self.sent = []

    def send(self, text):
        self.sent.append(text)
        return True


def runner(settings, client, storage):
    return Runner(settings, client, storage, notifier=Notes())


def test_start_notice_at_most_once_per_hour(settings, client, storage):
    r = runner(settings, client, storage)
    r._start_notice(NOW, "started")
    r._start_notice(NOW + timedelta(minutes=20), "started")        # restart loop: suppressed
    r._start_notice(NOW + timedelta(minutes=40), "started")
    assert r.notify.sent == ["started"]
    r._start_notice(NOW + timedelta(hours=2), "started")
    assert len(r.notify.sent) == 2 and "2 unannounced restart(s)" in r.notify.sent[1]
    r._start_notice(NOW + timedelta(hours=4), "started")
    assert r.notify.sent[2] == "started"                            # counter was reset


def test_scoring_is_cached_for_the_interval(settings, client, storage):
    storage.insert_messages("lobby", [(1, (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"), DID_A, DID_A, True, "hi", "h1")], "x")
    r = runner(settings, client, storage)
    a = r.scored(NOW)
    assert DID_A in a and r.scored(NOW + timedelta(minutes=10)) is a
    assert r.scored(NOW + timedelta(minutes=10), fresh=True) is not a
    b = r.scored(NOW + timedelta(minutes=50))
    assert b is not a and r.scored(NOW + timedelta(minutes=55)) is b
