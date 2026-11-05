"""Colour themes (Settings > Appearance)."""
from doppleganger import brand
from doppleganger.config import Config


def test_default_is_ivory():
    assert Config().theme == "ivory"


def test_switching_sets_every_colour_role_and_back():
    roles = [k for k in brand.THEMES["ivory"] if k.isupper()]
    before = {k: getattr(brand, k) for k in roles} | {"CV_AQUA": brand.CV_AQUA}
    try:
        assert brand.set_theme("slate") == "slate"
        for k in roles:
            assert getattr(brand, k) == brand.THEMES["slate"][k]
        assert brand.CV_AQUA == brand.bgr(brand.THEMES["slate"]["cv"]["AQUA"])
        assert (brand.LIVE_WORD, brand.LOOP_WORD) == ("blue", "copper")
        assert brand.set_theme("no-such-theme") == "ivory"           # unknown -> the default
    finally:
        brand.set_theme("ivory")
    assert {k: getattr(brand, k) for k in roles} | {"CV_AQUA": brand.CV_AQUA} == before


def test_every_theme_defines_the_same_roles():
    keys = {k for k in brand.THEMES["ivory"]}
    for name, t in brand.THEMES.items():
        assert set(t) == keys, name
        assert set(t["cv"]) == set(brand.THEMES["ivory"]["cv"]), name
