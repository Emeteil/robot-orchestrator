from robot_orchestrator.sdnotify import SdNotifier


def test_notifier_without_notify_socket_is_a_safe_noop(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    notifier = SdNotifier()

    notifier.ready()
    notifier.watchdog()
    notifier.status("boot: probing hardware")
    notifier.stopping()
