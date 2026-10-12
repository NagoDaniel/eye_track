import app
import worker


def test_packages_import() -> None:
    assert app.__name__ == "app"
    assert worker.__name__ == "worker"
