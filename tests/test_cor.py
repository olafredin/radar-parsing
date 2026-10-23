from radar_parsing.cor import (
    REPAIR_ELEV_BAD,
    REPAIR_FUSED,
    REPAIR_NONE,
    REPAIR_SHIFTED,
    REPAIR_UNIT,
    normalize_fields,
    read_cor_table,
)

BASE = "80\t2025-12-13\t15:20:59\t71.92496376150\tS\t11.04844114783\tW"


def test_clean_line_untouched():
    c = normalize_fields(BASE + "\t38.944\tM\t0.6")
    assert c.repair == REPAIR_NONE
    assert c.fields[7:] == ["38.944", "M", "0.6"]


def test_garbled_unit():
    c = normalize_fields(BASE + "\t38.940\t,\t0.6")
    assert c.repair == REPAIR_UNIT
    assert c.fields[7:] == ["38.940", "M", "0.6"]


def test_fused_elevation_keeps_value():
    # The old Mala_fix_cor2.py turned this into elevation 0.6
    c = normalize_fields(BASE + "\t38.944M\t9\t0.6")
    assert c.repair == REPAIR_FUSED
    assert c.fields[7:] == ["38.944", "M", "0.6"]


def test_shifted_elevation():
    c = normalize_fields(BASE + "\tM\t9\t38.975")
    assert c.repair == REPAIR_SHIFTED
    assert c.fields[7:] == ["38.975", "M", "."]


def test_unrecoverable_elevation():
    c = normalize_fields(BASE + "\tM\t9\t0.68.965")
    assert c.repair == REPAIR_ELEV_BAD


def test_extra_and_short_lines():
    assert normalize_fields("") is None
    assert normalize_fields("1\t2\t3") is None
    c = normalize_fields(BASE + "\t38.9\tM\t0\t.6")
    assert len(c.fields) == 10 and c.fields[9] == "0.6"


def test_read_cor_table_signs(tmp_path):
    p = tmp_path / "x.cor"
    p.write_text(BASE + "\t38.944\tM\t0.6\r\n" + BASE.replace("\tS\t", "\tN\t").replace("\tW", "\tE") + "\t1\tM\t.\r\n")
    df = read_cor_table(p)
    assert list(df["lat"].round(3)) == [-71.925, 71.925]
    assert list(df["lon"].round(3)) == [-11.048, 11.048]
    assert df["quality"].isna().tolist() == [False, True]
