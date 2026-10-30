"""Testes dos leitores Gerber/Excellon e da detecção de arquivos."""
from __future__ import annotations

import math
import time
from pathlib import Path

import numpy as np
import pytest
import shapely

from brokcam.core.model import DrillLayer, GerberLayer
from brokcam.parsers import (
    ParseError,
    guess_role,
    load_file,
    parse_excellon,
    parse_excellon_text,
    parse_gerber,
    parse_gerber_text,
)

DATA = Path(__file__).parent / "data"


def gmm(body: str, fs: str = "%FSLAX46Y46*%") -> GerberLayer:
    """Gerber em mm (formato 4.6) com o corpo dado."""
    return parse_gerber_text(f"{fs}\n%MOMM*%\n{body}\nM02*\n", name="teste")


def c(v: float) -> str:
    return str(int(round(v * 1e6)))


def approx_rel(value: float, expected: float, rel: float = 5e-3) -> bool:
    return abs(value - expected) <= rel * abs(expected)


# =========================================================================== Gerber: aberturas

class TestApertures:
    def test_circle_flash(self):
        L = gmm(f"%ADD10C,1.000000*%\nD10*\nX{c(5)}Y{c(5)}D03*")
        assert approx_rel(L.copper.area, math.pi * 0.25)
        assert L.bounds == pytest.approx((4.5, 4.5, 5.5, 5.5), abs=1e-6)
        assert L.apertures["D10"].shape == "C"
        assert L.apertures["D10"].params == (1.0,)
        assert L.warnings == []

    def test_rect_obround_polygon(self):
        L = gmm("%ADD10R,2.000000X1.000000*%\n%ADD11O,2.000000X1.000000*%\n"
                "%ADD12P,1.800000X8X22.500000*%\n"
                "D10*\nX0Y0D03*\nD11*\nX10000000Y0D03*\nD12*\nX20000000Y0D03*")
        parts = sorted(L.copper.geoms, key=lambda p: p.bounds[0])
        assert parts[0].area == pytest.approx(2.0)
        assert approx_rel(parts[1].area, 1.0 + math.pi * 0.25)
        assert parts[1].bounds == pytest.approx((9.0, -0.5, 11.0, 0.5), abs=1e-9)
        octagon = 4 * 0.9 ** 2 * math.sin(math.pi / 4)
        assert parts[2].area == pytest.approx(octagon, rel=1e-9)

    def test_aperture_hole(self):
        L = gmm("%ADD10R,2.0X2.0X1.0*%\nD10*\nX0Y0D03*")
        assert approx_rel(L.copper.area, 4.0 - math.pi * 0.25)
        assert len(L.copper.interiors) == 1

    def test_inch_units_conversion(self):
        L = parse_gerber_text("%FSLAX24Y24*%\n%MOIN*%\n%ADD10R,0.1X0.1*%\nD10*\nX10000Y10000D03*\nM02*")
        assert L.copper.area == pytest.approx(2.54 ** 2)
        assert L.bounds == pytest.approx((24.13, 24.13, 26.67, 26.67))
        assert L.apertures["D10"].params == pytest.approx((2.54, 2.54))

    def test_trailing_zero_format(self):
        L = gmm("%ADD10C,1.0*%\nD10*\nX01Y02D03*", fs="%FSTAX33Y33*%")
        cx, cy = L.copper.centroid.coords[0]
        assert (cx, cy) == pytest.approx((10.0, 20.0), abs=1e-6)

    def test_incremental_coordinates(self):
        L = gmm("%ADD10C,1.0*%\nD10*\nX1000Y1000D03*\nX1000Y0D03*\nX1000Y0D03*", fs="%FSLIX33Y33*%")
        xs = sorted(round(p.centroid.x, 6) for p in L.copper.geoms)
        assert xs == [1.0, 2.0, 3.0]

    def test_quad_segs_resolution(self):
        lo = parse_gerber_text("%FSLAX46Y46*%%MOMM*%%ADD10C,1.0*%D10*X0Y0D03*M02*", quad_segs=4)
        hi = parse_gerber_text("%FSLAX46Y46*%%MOMM*%%ADD10C,1.0*%D10*X0Y0D03*M02*", quad_segs=32)
        assert len(lo.copper.exterior.coords) < len(hi.copper.exterior.coords)
        assert abs(hi.copper.area - math.pi * 0.25) < abs(lo.copper.area - math.pi * 0.25)


# =========================================================================== Gerber: trilhas

class TestTracks:
    def test_straight_track_area_and_centerline(self):
        L = gmm(f"%ADD10C,0.200000*%\nD10*\nX0Y0D02*\nX{c(10)}Y0D01*")
        assert approx_rel(L.copper.area, 10 * 0.2 + math.pi * 0.01)
        assert len(L.traces) == 1
        np.testing.assert_allclose(L.traces[0], [[0, 0], [10, 0]])

    def test_polyline_continues_and_breaks(self):
        L = gmm(f"%ADD10C,0.2*%\n%ADD11C,0.4*%\nD10*\nX0Y0D02*\nX{c(5)}Y0D01*\nX{c(5)}Y{c(5)}D01*\n"
                f"D11*\nX{c(10)}Y{c(5)}D01*\nX{c(20)}Y0D02*\nX{c(25)}Y0D01*")
        assert [len(t) for t in L.traces] == [3, 2, 2]

    def test_rect_aperture_sweep(self):
        L = gmm(f"%ADD10R,1.0X1.0*%\nD10*\nX0Y0D02*\nX{c(10)}Y0D01*")
        assert L.copper.area == pytest.approx(11.0)
        assert L.traces == []

    def test_multi_quadrant_arc(self):
        # quarto de círculo r=5 de (5,0) a (0,5), centro na origem, largura 0.2
        L = gmm(f"%ADD10C,0.2*%\nD10*\nG75*\nX{c(5)}Y0D02*\nG03X0Y{c(5)}I{c(-5)}J0D01*")
        arc = L.traces[0]
        r = np.hypot(arc[:, 0], arc[:, 1])
        assert np.allclose(r, 5.0, atol=1e-9)
        np.testing.assert_allclose(arc[-1], [0, 5])
        # tolerância de corda padrão 0.005 mm
        mids = (arc[1:] + arc[:-1]) / 2
        assert (5.0 - np.hypot(mids[:, 0], mids[:, 1])).max() <= 0.0051
        assert approx_rel(L.copper.area, math.pi * 5 / 2 * 0.2 + math.pi * 0.01, 2e-3)

    def test_clockwise_full_circle(self):
        L = gmm(f"%ADD10C,0.250000*%\nD10*\nG75*\nX{c(1.5)}Y0D02*\nG02X{c(1.5)}Y0I{c(-1.5)}J0D01*")
        assert approx_rel(L.copper.area, 2 * math.pi * 1.5 * 0.25, 2e-3)
        assert len(L.copper.interiors) == 1

    def test_single_quadrant_arc(self):
        # G74: I/J sem sinal; arco horário de (0,5) a (5,0) com centro (0,0)
        L = gmm(f"%ADD10C,0.1*%\nD10*\nG74*\nX0Y{c(5)}D02*\nG02X{c(5)}Y0I0J{c(5)}D01*")
        arc = L.traces[0]
        assert np.allclose(np.hypot(arc[:, 0], arc[:, 1]), 5.0, atol=1e-9)
        assert arc[:, 0].min() >= -1e-9 and arc[:, 1].min() >= -1e-9

    def test_zero_length_track_is_dot(self):
        L = gmm("%ADD10C,1.0*%\nD10*\nX0Y0D02*\nX0Y0D01*")
        assert approx_rel(L.copper.area, math.pi * 0.25)


# =========================================================================== Gerber: regiões e polaridade

class TestRegionsPolarity:
    def test_region_with_arc(self):
        # meio disco r=2: (2,0) -> arco anti-horário -> (-2,0) -> volta
        L = gmm(f"G36*\nX{c(2)}Y0D02*\nG75*\nG03X{c(-2)}Y0I{c(-2)}J0D01*\nG01*\nX{c(2)}Y0D01*\nG37*")
        assert approx_rel(L.copper.area, math.pi * 2, 5e-3)
        assert L.traces == []

    def test_region_multiple_contours(self):
        body = ("G36*\nX0Y0D02*\nG01*\nX1000000Y0D01*\nX1000000Y1000000D01*\nX0Y1000000D01*\nX0Y0D01*\n"
                "X5000000Y0D02*\nX6000000Y0D01*\nX6000000Y2000000D01*\nX5000000Y2000000D01*\n"
                "X5000000Y0D01*\nG37*")
        L = gmm(body)
        assert L.copper.area == pytest.approx(3.0)
        assert len(L.copper.geoms) == 2

    def test_self_intersecting_region_is_repaired(self):
        L = gmm("G36*\nX0Y0D02*\nG01*\nX2000000Y2000000D01*\nX2000000Y0D01*\nX0Y2000000D01*\n"
                "X0Y0D01*\nG37*")
        assert L.copper.is_valid
        assert L.copper.area == pytest.approx(2.0)

    def test_lpc_file(self):
        L = parse_gerber(DATA / "lpc_test.gbr")
        expected = 100 - math.pi * 4 + 1
        assert approx_rel(L.copper.area, expected, 2e-3)
        assert L.copper.contains(shapely.Point(5, 5))        # ilha LPD após o LPC
        assert not L.copper.contains(shapely.Point(5, 6.5))  # área limpa pelo LPC
        assert L.copper.contains(shapely.Point(1, 1))

    def test_clear_only_affects_previous_objects(self):
        body = ("%ADD10R,4X4*%\n%ADD11R,2X2*%\n%LPC*%\nD11*\nX0Y0D03*\n%LPD*%\nD10*\nX0Y0D03*")
        L = gmm(body, fs="%FSLAX33Y33*%")
        assert L.copper.area == pytest.approx(16.0)

    def test_step_and_repeat(self):
        body = ("%SRX3Y2I5.0J4.0*%\n%ADD10R,1.0X1.0*%\nD10*\nX0Y0D03*\n"
                f"%ADD11C,0.1*%\nD11*\nX0Y0D02*\nX{c(1)}Y0D01*\n%SR*%")
        L = gmm(body)
        assert len(L.copper.geoms) == 6
        assert L.bounds == pytest.approx((-0.5, -0.5, 11.05, 4.5))
        assert len(L.traces) == 6

    def test_aperture_block(self):
        body = ("%ADD10C,1.0*%\n%ADD11R,3X3*%\n%ABD20*%\nD11*\nX0Y0D03*\n%LPC*%\nD10*\nX0Y0D03*\n"
                "%LPD*%\n%AB*%\nD20*\nX10000000Y0D03*\nX20000000Y0D03*")
        L = gmm(body)
        assert approx_rel(L.copper.area, 2 * (9 - math.pi * 0.25))
        assert L.bounds == pytest.approx((8.5, -1.5, 21.5, 1.5))


# =========================================================================== Gerber: macros

class TestMacros:
    def test_expressions_and_variables(self):
        body = ("%AMBOX*\n0 retangulo com variaveis*\n$3=$1x2*\n$4=($2+$1)/2-0.5*\n"
                "21,1,$3,$4,0,0,0*%\n%ADD10BOX,1.0X2.0*%\nD10*\nX0Y0D03*")
        L = gmm(body)
        assert L.bounds == pytest.approx((-1.0, -0.5, 1.0, 0.5))

    def test_kicad_roundrect(self):
        L = parse_gerber(DATA / "board-F_Cu.gbr")
        # pad RoundRect em (11.5, 10): corpo 1.2x0.9 + raio 0.25 -> 1.7x1.4 com cantos redondos
        cx, cy = 11.5 + 0.6, 10.0 + 0.45     # centro do círculo do canto superior direito
        assert L.copper.contains(shapely.Point(cx + 0.15, cy + 0.15))
        assert not L.copper.contains(shapely.Point(cx + 0.2, cy + 0.2))
        assert L.apertures["D13"].shape == "RoundRect"

    def test_primitives(self):
        body = (
            "%AMPRIMS*\n"
            "1,1,2.0,0,0*\n"                 # círculo d=2
            "1,0,1.0,0,0*\n"                 # furo (exposição desligada)
            "20,1,0.5,3,0,7,0,0*\n"          # linha vetorial 4x0.5
            "21,1,1,2,10,0,0*\n"             # linha central 1x2
            "4,1,3,12,0,14,0,12,2,12,0,0*\n"  # triângulo área 2
            "5,1,4,20,0,2,0*%\n"             # quadrado (diag 2) área 2
            "%ADD10PRIMS*%\nD10*\nX0Y0D03*")
        L = gmm(body)
        expected = math.pi * (1 - 0.25) + 2.0 + 2.0 + 2.0 + 2.0
        assert approx_rel(L.copper.area, expected, 3e-3)

    def test_rotation_about_macro_origin(self):
        body = "%AMROT*\n21,1,2,1,3,0,90*%\n%ADD10ROT*%\nD10*\nX0Y0D03*"
        L = gmm(body)
        assert L.bounds == pytest.approx((-0.5, 2.0, 0.5, 4.0), abs=1e-9)

    def test_thermal_primitive(self):
        L = parse_gerber_text("%FSLAX46Y46*%%MOMM*%%AMTH*7,0,0,2.2,1.6,0.4,0*%%ADD10TH*%D10*X0Y0D03*M02*")
        assert len(L.copper.geoms) == 4
        approx = math.pi * (1.1 ** 2 - 0.8 ** 2) - 4 * 0.4 * 0.3
        assert approx_rel(L.copper.area, approx, 0.03)

    def test_eagle_oc8_macro_inch(self):
        L = parse_gerber(DATA / "eagle_board.cmp")
        assert L.warnings == []
        assert L.apertures["D11"].shape == "OC8"
        # pad OC8 de 0.06" em (0.8", 0.2") — octógono de lado-a-lado 0.06"
        pad = L.copper.intersection(shapely.box(19.0, 3.5, 21.6, 6.5))
        assert pad.bounds[1] == pytest.approx(5.08 - 0.762, abs=1e-3)


# =========================================================================== Gerber: arquivos de exemplo

class TestSampleBoard:
    def test_front_copper(self):
        L = parse_gerber(DATA / "board-F_Cu.gbr")
        assert L.warnings == []
        assert L.copper.is_valid and L.copper.geom_type == "MultiPolygon"
        assert L.bounds == pytest.approx((3.15, 1.0, 29.0, 15.4), abs=1e-6)
        for x, y in [(4, 7.46), (4, 10), (4, 12.54), (26, 6), (26, 14), (11.5, 10), (14.5, 10),
                     (20, 10), (22.5, 10), (8, 15), (18, 5), (26, 10)]:
            assert L.copper.contains(shapely.Point(x, y)), (x, y)
        # zona G36 com canto arredondado
        zone = L.copper.intersection(shapely.box(20.9, 0.9, 29.1, 4.1))
        assert approx_rel(zone.area, 24 - (1 - math.pi / 4), 1e-3)
        # anel completo de r=1.5 e largura 0.25
        ring = L.copper.intersection(shapely.box(6.3, 2.3, 9.7, 5.7))
        assert approx_rel(ring.area, 2 * math.pi * 1.5 * 0.25, 3e-3)
        assert len(L.traces) == 6
        assert set(L.apertures) == {f"D{i}" for i in range(10, 19)}

    def test_back_copper_with_thermal(self):
        L = parse_gerber(DATA / "board-B_Cu.gbr")
        assert L.warnings == []
        assert L.copper.is_valid
        assert L.copper.contains(shapely.Point(8, 13))

    def test_edge_cuts(self):
        L = parse_gerber(DATA / "board-Edge_Cuts.gbr")
        assert L.bounds == pytest.approx((-0.05, -0.05, 30.05, 20.05), abs=1e-6)
        assert L.copper.geom_type == "Polygon"
        inner = shapely.Polygon(L.copper.interiors[0])
        assert approx_rel(inner.area, 29.9 * 19.9 - (4 - math.pi) * 0.95 ** 2, 5e-4)
        assert len(L.traces) == 1 and np.allclose(L.traces[0][0], L.traces[0][-1])

    def test_eagle_dimension_inch(self):
        L = parse_gerber(DATA / "eagle_board.dim")
        assert L.bounds == pytest.approx((-0.1016, -0.1016, 30.5816, 20.4216), abs=1e-6)


# =========================================================================== Gerber: robustez

class TestGerberRobustness:
    def test_missing_format_and_units_warns(self):
        L = parse_gerber_text("%ADD10C,0.01*%\nD10*\nX10000Y10000D03*\nM02*")
        assert any("%FS" in w for w in L.warnings)
        assert any("%MO" in w for w in L.warnings)
        assert L.bounds[0] == pytest.approx(25.4 - 0.127)

    def test_x2_attributes_and_comments_ignored(self):
        body = ("%TF.FileFunction,Copper,L1,Top*%\n%TA.AperFunction,SMDPad*%\n%ADD10C,1*%\n%TD*%\n"
                "G04 comentario com X123Y456D01 dentro*\n%TO.N,GND*%\nD10*\nX0Y0D03*\n%TD*%")
        L = gmm(body)
        assert L.warnings == []
        assert len(L.copper.geoms if hasattr(L.copper, "geoms") else [L.copper]) == 1

    def test_deprecated_modal_coordinates(self):
        L = gmm("%ADD10C,1*%\nD10*\nX0Y0D03*\nX5000000Y0*\nX10000000Y0*")
        assert len(L.copper.geoms) == 3
        assert any("modal" in w for w in L.warnings)

    def test_undefined_aperture_warns(self):
        L = gmm("%ADD10C,1*%\nD11*\nX0Y0D03*\nD10*\nX0Y0D03*")
        assert any("D11" in w for w in L.warnings)
        assert approx_rel(L.copper.area, math.pi * 0.25)

    def test_not_gerber_raises(self):
        with pytest.raises(ParseError):
            parse_gerber_text("isto não é um gerber\napenas texto")

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(ParseError, match="não encontrado"):
            parse_gerber(tmp_path / "nao_existe.gbr")


# =========================================================================== Excellon

class TestExcellon:
    def test_kicad_pth(self):
        D = parse_excellon(DATA / "board-PTH.drl")
        assert D.warnings == []
        assert {n: len(t.points) for n, t in D.tools.items()} == {1: 2, 2: 3, 3: 3}
        assert {n: t.diameter for n, t in D.tools.items()} == {1: 0.4, 2: 1.0, 3: 0.8}
        assert all(t.plated is True for t in D.tools.values())
        np.testing.assert_allclose(D.tools[2].points[1], [4.0, 10.0])

    def test_kicad_npth_slots(self):
        D = parse_excellon(DATA / "board-NPTH.drl")
        assert D.warnings == []
        assert len(D.tools[1].points) == 2 and D.tools[1].diameter == 3.2
        assert D.tools[2].points.shape == (0, 2)
        np.testing.assert_allclose(D.tools[2].slots, [[12, 16, 16, 16], [13, 3, 15, 3]])
        assert all(t.plated is False for t in D.tools.values())

    def test_eagle_inch_modal(self):
        D = parse_excellon(DATA / "eagle_board.drd")
        assert D.tools[1].diameter == pytest.approx(0.0236 * 25.4)
        np.testing.assert_allclose(D.tools[1].points, [[20.32, 5.08], [25.4, 5.08], [25.4, 15.24]])
        np.testing.assert_allclose(D.tools[2].points, [[2.54, 2.54], [27.94, 2.54]])

    def test_leading_zeros_format(self):
        D = parse_excellon_text("M48\nINCH,LZ\nT1C0.035\n%\nT1\nX012500Y-0025\nM30\n")
        np.testing.assert_allclose(D.tools[1].points, [[1.25 * 25.4, -0.25 * 25.4]])

    def test_trailing_zeros_metric_format(self):
        D = parse_excellon_text("M48\nMETRIC,TZ,000.000\nT1C0.8\n%\nT1\nX12500Y3000\nM30\n")
        np.testing.assert_allclose(D.tools[1].points, [[12.5, 3.0]])

    def test_file_format_comment_and_metric_format(self):
        D = parse_excellon_text("M48\n;FILE_FORMAT=4:4\nMETRIC,LZ\nT1C1.0\n%\nT1\nX00125000Y00005\nM30\n")
        np.testing.assert_allclose(D.tools[1].points, [[12.5, 0.5]])
        D2 = parse_excellon_text("M48\nMETRIC,LZ,0000.00\nT01C0.8F200S65\n%\nT01\nX001250Y000050\nM30\n")
        np.testing.assert_allclose(D2.tools[1].points, [[12.5, 0.5]])

    def test_tool_defined_in_body_and_altium_plated(self):
        txt = ("M48\n;TYPE=PLATED\nMETRIC\nT1F00S00C0.300\n%\nT01\nX1.0Y1.0\nT2C1.2\nX2.0Y2.0\n"
               "T0\nM30\n")
        D = parse_excellon_text(txt)
        assert D.tools[2].diameter == 1.2
        np.testing.assert_allclose(D.tools[2].points, [[2, 2]])
        assert D.tools[1].plated is True and D.tools[2].plated is True

    def test_incremental_mode(self):
        D = parse_excellon_text("M48\nMETRIC\nICI,ON\nT1C1\n%\nT1\nX1.0Y1.0\nX1.0\nY1.0\nM30\n")
        np.testing.assert_allclose(D.tools[1].points, [[1, 1], [2, 1], [2, 2]])

    def test_npth_from_filename(self):
        D = parse_excellon_text("M48\nMETRIC\nT1C3.0\n%\nT1\nX1.0Y1.0\nM30\n", source_path="x-NPTH.drl")
        assert D.tools[1].plated is False

    def test_bounds_include_slots(self):
        D = parse_excellon(DATA / "board-NPTH.drl")
        assert D.bounds == pytest.approx((3.0, 3.0, 27.0, 17.0))

    def test_not_excellon_raises(self):
        with pytest.raises(ParseError):
            parse_excellon_text("olá mundo\nnada aqui")


# =========================================================================== Detecção

class TestDetect:
    @pytest.mark.parametrize("fname,kind", [
        ("board-F_Cu.gbr", "gerber"), ("board-B_Cu.gbr", "gerber"),
        ("board-Edge_Cuts.gbr", "gerber"), ("board-PTH.drl", "excellon"),
        ("board-NPTH.drl", "excellon"), ("eagle_board.cmp", "gerber"),
        ("eagle_board.drd", "excellon"), ("eagle_board.dim", "gerber"), ("lpc_test.gbr", "gerber"),
    ])
    def test_load_samples(self, fname, kind):
        layer = load_file(DATA / fname)
        assert layer.kind == kind
        assert isinstance(layer, GerberLayer if kind == "gerber" else DrillLayer)

    def test_content_sniffing_for_ambiguous_extensions(self, tmp_path):
        g = tmp_path / "camada.txt"
        g.write_text((DATA / "lpc_test.gbr").read_text())
        assert load_file(g).kind == "gerber"
        d = tmp_path / "furos.nc"
        d.write_text((DATA / "board-PTH.drl").read_text())
        assert load_file(d).kind == "excellon"

    def test_errors_in_portuguese(self, tmp_path):
        with pytest.raises(ParseError, match="não encontrado"):
            load_file(tmp_path / "x.gbr")
        empty = tmp_path / "vazio.gbr"
        empty.write_text("")
        with pytest.raises(ParseError, match="vazio"):
            load_file(empty)
        junk = tmp_path / "lixo.dat"
        junk.write_text("lorem ipsum\n")
        with pytest.raises(ParseError, match="formato"):
            load_file(junk)
        with pytest.raises(ParseError, match="pasta"):
            load_file(tmp_path)

    @pytest.mark.parametrize("name,role", [
        # KiCad
        ("board-F_Cu.gbr", "top_copper"), ("board-B_Cu.gbr", "bottom_copper"),
        ("board-Edge_Cuts.gbr", "outline"), ("board-F_SilkS.gbr", "silk"),
        ("board-F_Silkscreen.gbr", "silk"), ("board-B_Mask.gbr", "mask"),
        ("board-F_Paste.gbr", "paste"), ("board-PTH.drl", "drill"), ("board-NPTH.drl", "drill"),
        ("board-In1_Cu.gbr", "unknown"), ("board.drl", "drill"),
        # Protel/Altium
        ("placa.GTL", "top_copper"), ("placa.GBL", "bottom_copper"), ("placa.GKO", "outline"),
        ("placa.GM1", "outline"), ("placa.GTO", "silk"), ("placa.GBS", "mask"),
        ("placa.GTP", "paste"), ("placa.TXT", "drill"), ("placa-RoundHoles.TXT", "drill"),
        # Eagle
        ("board.cmp", "top_copper"), ("board.sol", "bottom_copper"), ("board.dim", "outline"),
        ("board.plc", "silk"), ("board.stc", "mask"), ("board.crc", "paste"), ("board.drd", "drill"),
        # EasyEDA
        ("Gerber_TopLayer.GTL", "top_copper"), ("Gerber_BottomLayer.GBL", "bottom_copper"),
        ("Gerber_BoardOutlineLayer.GKO", "outline"), ("Drill_PTH_Through.DRL", "drill"),
        ("Drill_NPTH_Through.DRL", "drill"), ("Gerber_TopSolderMaskLayer.GTS", "mask"),
        ("Gerber_TopPasteMaskLayer.GTP", "paste"), ("Gerber_TopSilkscreenLayer.GTO", "silk"),
        ("Gerber_TopLayer.gbr", "top_copper"), ("Gerber_TopSolderMaskLayer.gbr", "mask"),
        ("Gerber_BoardOutlineLayer.gbr", "outline"),
        # genéricos
        ("top.gbr", "top_copper"), ("bottom.gbr", "bottom_copper"), ("outline.gbr", "outline"),
        ("readme.md", "unknown"), ("C:/proj/saida/board-F_Cu.gbr", "top_copper"),
    ])
    def test_guess_role(self, name, role):
        assert guess_role(name) == role


# =========================================================================== Desempenho

def _synthetic_gerber(nx: int = 100, ny: int = 100) -> tuple[str, int]:
    """~nx*ny flashes (3 tipos de abertura, incluindo macro) + nx*ny trilhas."""
    head = [
        "%FSLAX46Y46*%", "%MOMM*%",
        "%AMRoundRect*1,1,$1+$1,$2,$3*1,1,$1+$1,$4,$5*1,1,$1+$1,$6,$7*1,1,$1+$1,$8,$9*"
        "4,1,4,$2,$3,$4,$5,$6,$7,$8,$9,$2,$3,0*%",
        "%ADD10C,0.800000*%", "%ADD11R,1.000000X0.600000*%",
        "%ADD12RoundRect,0.1X-0.3X-0.2X0.3X-0.2X0.3X0.2X-0.3X0.2X0*%",
        "%ADD13C,0.200000*%",
    ]
    parts: list[str] = []
    pitch = 2.0
    count = 0
    for k, ap in enumerate(("D10*", "D11*", "D12*")):
        parts.append(ap)
        for i in range(nx):
            for j in range(ny // 3 + 1):
                parts.append(f"X{c(i * pitch)}Y{c(j * pitch * 3 + k * pitch)}D03*")
                count += 1
    parts.append("D13*")
    for i in range(nx):
        for j in range(ny):
            x0, y0 = i * pitch, j * pitch
            # trilha liga o pad (x0,y0) à diagonal: toca pads -> exige união real
            parts.append(f"X{c(x0)}Y{c(y0)}D02*X{c(x0 + 1)}Y{c(y0 + 1)}D01*")
            count += 1
    return "\n".join(head + parts) + "\nM02*\n", count


def test_performance_20k_primitives():
    text, count = _synthetic_gerber()
    assert count >= 20000
    t0 = time.perf_counter()
    L = parse_gerber_text(text, name="sintetico")
    dt = time.perf_counter() - t0
    print(f"\n[desempenho] {count} primitivas em {dt:.2f} s")
    assert L.copper.is_valid
    assert len(L.traces) == 10000
    assert dt < 3.0, f"parse levou {dt:.2f} s (limite 3 s)"
