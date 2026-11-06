"""Run against a checkout of Cardinal with its dependencies installed."""
import os
import shutil
import sys
import tempfile
from configparser import ConfigParser
from pathlib import Path
from types import SimpleNamespace

source = Path(os.environ["CARDINAL_SOURCE"]).resolve()
plugin = Path(__file__).resolve().parents[1] / "greengamepay.py"
sys.path.insert(0, str(source))
# Import only; do not instantiate Cardinal, start a bot or authenticate.
from cardinal import Cardinal  # noqa: E402
from FunPayAPI import types  # noqa: E402
from handlers import setup_event_attributes_handler  # noqa: E402

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
        assert callable(module.BIND_TO_POST_START[0])
        print("Cardinal loader: PASS", metadata["NAME"], metadata["VERSION"])
        category = types.SubCategory(1, "Steam", types.SubCategoryTypes.COMMON, types.Category(1, "Steam"))
        old = types.UserProfile(1, "seller", "", True, False, "")
        fresh = types.UserProfile(1, "seller", "", True, False, "")
        historical = SimpleNamespace(id=123, subcategory=category, description="Inactive old lot", server=None, side=None)
        new = SimpleNamespace(id=456, subcategory=category, description="New lot after startup", server=None, side=None)
        old.update_lot(historical)
        fresh.update_lot(new)
        c = SimpleNamespace(profile=old, account=SimpleNamespace(id=1, get_user=lambda user_id: fresh),
                            telegram=None, AD_CFG=ConfigParser())
        engine = module.Engine(c, Path(directory) / "storage-test")
        try:
            assert set(engine.refresh_lots(force=True)) == {456}
            event = SimpleNamespace(order=SimpleNamespace(description=new.description, subcategory=category))
            setup_event_attributes_handler(c, event)
            assert event.lot_id == 456
            assert c.profile.get_lot(123) is historical
            print("Cardinal fresh lot matching: PASS (historical offers retained)")
        finally:
            engine.close()
    finally:
        os.chdir(previous)
