"""Run against a checkout of Cardinal with its dependencies installed."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

source = Path(os.environ["CARDINAL_SOURCE"]).resolve()
plugin = Path(__file__).resolve().parents[1] / "greengamepay.py"
sys.path.insert(0, str(source))
# Import only; do not instantiate Cardinal, start a bot or authenticate.
from cardinal import Cardinal  # noqa: E402

with tempfile.TemporaryDirectory() as directory:
    previous = Path.cwd()
    try:
        os.chdir(directory)
        Path("plugins").mkdir()
        shutil.copy2(plugin, "plugins/greengamepay.py")
        assert Cardinal.is_plugin("greengamepay.py")
        module, metadata = Cardinal.load_plugin("greengamepay.py")
        assert Cardinal.is_uuid_valid(metadata["UUID"])
        assert metadata["SETTINGS_PAGE"] is True
        assert callable(metadata["BIND_TO_DELETE"])
        assert callable(module.BIND_TO_NEW_ORDER[0])
        assert callable(module.BIND_TO_NEW_MESSAGE[0])
        print("Cardinal loader: PASS", metadata["NAME"], metadata["VERSION"])
    finally:
        os.chdir(previous)
