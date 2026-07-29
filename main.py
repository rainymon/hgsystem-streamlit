from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st


APP_DIR = Path(__file__).resolve().parent
DEFAULT_HGSYSTEM_DIR = APP_DIR

HPR_COLUMNS = [
    "distance_m", "plume_height_m", "effective_diameter_m", "velocity_m_s",
    "axis_angle_deg", "temperature_c", "hf_vol_pct", "hydrocarbon_vol_pct",
    "plume_density_kg_m3", "hf_mass_conc_kg_m3", "vapour_mass_pct",
    "gas_void_pct", "total_plume_massflow_kg_s",
]
HSR_COLUMNS = [
    "distance_m", "pollutant_vol_pct", "sz_m", "sy_m", "midp_m",
    "y_upper_m", "z_upper_m", "y_lower_m", "z_lower_m", "richardson",
    "temperature_c", "pollutant_mass_conc_kg_m3", "aerosol_vol_pct",
]
HSF_COLUMNS = [
    "distance_m", "framol", "y11_monomer", "y12_dimer", "y16_hexamer",
    "y18_octamer", "yc_complex", "y2_water", "ya_air", "temperature_c",
    "xl_hf_in_fog", "lfog_moles", "fog_status",
]


@dataclass
class RunResult:
    command: str
    returncode: int
    stdout: str
    stderr: str


def read_text_flexible(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "cp949", "mbcs" if os.name == "nt" else "latin-1", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("latin-1", errors="replace")


def write_ascii(path: Path, text: str) -> None:
    path.write_bytes(text.encode("ascii", errors="replace"))


def sanitize_case_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", value.upper())[:8]
    return cleaned or "CASE01"


def sanitize_title(value: str) -> str:
    value = value.strip() or "HFPLUME Streamlit case"
    return value.encode("ascii", errors="replace").decode("ascii")[:70]


def set_title(text: str, title: str) -> str:
    replacement = f"TITLE   {sanitize_title(title)}"
    updated, count = re.subn(r"(?mi)^TITLE\s+.*$", replacement, text, count=1)
    if count == 0:
        updated = replacement + "\r\n" + text
    return updated


def replace_parameter(text: str, key: str, value: Any) -> tuple[str, bool]:
    if isinstance(value, float):
        formatted = f"{value:.10g}"
    else:
        formatted = str(value)
    pattern = re.compile(rf"(?mi)^(\s*{re.escape(key)}\s*=\s*)([^\s*]+)")
    updated, count = pattern.subn(lambda m: m.group(1) + formatted, text, count=1)
    return updated, count > 0


def build_hpi(template_path: Path, output_path: Path, title: str, values: dict[str, Any]) -> list[str]:
    text = read_text_flexible(template_path)
    text = set_title(text, title)
    missing: list[str] = []
    for key, value in values.items():
        text, found = replace_parameter(text, key, value)
        if not found:
            missing.append(key)
    # Old executables are safest with CRLF and ASCII.
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
    write_ascii(output_path, text)
    return missing


def patch_hsl(path: Path, comin: float, clv: float, cuv: float) -> list[str]:
    text = read_text_flexible(path)
    original = text
    missing: list[str] = []
    for key, value in {"COMIN": comin, "CLV": clv, "CUV": cuv}.items():
        text, found = replace_parameter(text, key, value)
        if not found:
            missing.append(key)
    if text != original:
        backup = path.with_suffix(".HL0")
        if not backup.exists():
            shutil.copy2(path, backup)
        text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")
        write_ascii(path, text)
    return missing


def run_batch(root: Path, work: Path, batch_name: str, args: list[str], timeout_s: int = 300) -> RunResult:
    if os.name != "nt":
        raise RuntimeError("HGSYSTEM 실행은 Windows에서만 지원됩니다.")
    batch = root / batch_name
    if not batch.exists():
        raise FileNotFoundError(f"{batch_name}을 찾을 수 없습니다: {batch}")

    env = os.environ.copy()
    env["HGSYSTEM"] = str(root)
    arg_text = " ".join(f'"{arg}"' if " " in arg else arg for arg in args)
    command_text = f'call "{batch}" {arg_text}'
    completed = subprocess.run(
        ["cmd.exe", "/d", "/s", "/c", command_text],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        timeout=timeout_s,
        check=False,
    )
    return RunResult(command_text, completed.returncode, completed.stdout, completed.stderr)


def numeric_tokens(line: str) -> list[float] | None:
    parts = line.split()
    values: list[float] = []
    for token in parts:
        try:
            values.append(float(token))
        except ValueError:
            return None
    return values


def parse_hpr(path: Path) -> pd.DataFrame:
    rows: list[list[float]] = []
    for line in read_text_flexible(path).splitlines():
        values = numeric_tokens(line)
        if values and len(values) == len(HPR_COLUMNS):
            rows.append(values)
    if not rows:
        return pd.DataFrame(columns=HPR_COLUMNS)
    df = pd.DataFrame(rows, columns=HPR_COLUMNS)
    df = df.drop_duplicates(subset=HPR_COLUMNS, keep="first").reset_index(drop=True)
    df["hf_ppm"] = df["hf_vol_pct"] * 10_000.0
    return df


def parse_hsr(path: Path) -> pd.DataFrame:
    rows: list[list[float]] = []
    for line in read_text_flexible(path).splitlines():
        values = numeric_tokens(line)
        if values and len(values) == len(HSR_COLUMNS):
            rows.append(values)
    if not rows:
        return pd.DataFrame(columns=HSR_COLUMNS)
    df = pd.DataFrame(rows, columns=HSR_COLUMNS)
    df = df.drop_duplicates(subset=HSR_COLUMNS, keep="first").reset_index(drop=True)
    df["pollutant_ppm"] = df["pollutant_vol_pct"] * 10_000.0
    return df


def as_float(token: str) -> float | None:
    if token in {"-", "--", ""}:
        return None
    try:
        return float(token)
    except ValueError:
        return None


def parse_hsf(path: Path) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for line in read_text_flexible(path).splitlines():
        parts = line.split()
        if len(parts) < 12:
            continue
        if as_float(parts[0]) is None:
            continue
        numeric = [as_float(token) for token in parts[:12]]
        if numeric[0] is None or numeric[1] is None:
            continue
        fog_status = parts[12] if len(parts) >= 13 else ""
        row = dict(zip(HSF_COLUMNS[:-1], numeric))
        row["fog_status"] = fog_status
        rows.append(row)
    if not rows:
        return pd.DataFrame(columns=HSF_COLUMNS)

    df = pd.DataFrame(rows, columns=HSF_COLUMNS)
    # Monomer-equivalent approximation. Kept explicit so it can be audited.
    df["hf_gas_equiv"] = (
        df["y11_monomer"].fillna(0)
        + 2 * df["y12_dimer"].fillna(0)
        + 6 * df["y16_hexamer"].fillna(0)
        + 8 * df["y18_octamer"].fillna(0)
        + df["yc_complex"].fillna(0)
    )
    df["hf_fog_equiv"] = df["xl_hf_in_fog"].fillna(0) * df["lfog_moles"].fillna(0)
    total = df["hf_gas_equiv"] + df["hf_fog_equiv"]
    df["hf_gas_pct_est"] = (100 * df["hf_gas_equiv"] / total).where(total > 0)
    df["hf_fog_pct_est"] = (100 * df["hf_fog_equiv"] / total).where(total > 0)
    return df


def first_crossing_distance(df: pd.DataFrame, x_col: str, y_col: str, target: float) -> float | None:
    if df.empty:
        return None
    data = df[[x_col, y_col]].dropna().sort_values(x_col).reset_index(drop=True)
    if data.empty:
        return None
    exact = data[data[y_col] == target]
    if not exact.empty:
        return float(exact.iloc[0][x_col])
    for i in range(1, len(data)):
        x1, y1 = data.iloc[i - 1]
        x2, y2 = data.iloc[i]
        if (y1 - target) * (y2 - target) < 0 and y2 != y1:
            return float(x1 + (target - y1) * (x2 - x1) / (y2 - y1))
    return None


def dataframe_csv_bytes(df: pd.DataFrame) -> bytes:
    return df.to_csv(index=False).encode("utf-8-sig")


def result_zip_bytes(paths: list[Path]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in paths:
            if path.exists() and path.is_file():
                zf.write(path, arcname=path.name)
    return buf.getvalue()


def validate_install(root: Path) -> list[str]:
    required = [
        "HFPLUME.BAT", "HEGADASS.BAT", "FFMAIN.EXE", "HPMAIN.EXE", "HSMAIN.EXE",
        "DICT.HPD", "DICT.HSD", "STINPUT/SKELETON.HPI",
    ]
    return [name for name in required if not (root / name).exists()]


def render_log(title: str, result: RunResult | None) -> None:
    if not result:
        return
    with st.expander(title, expanded=False):
        st.code(f"> {result.command}\n\n{result.stdout}\n{result.stderr}".strip(), language="text")


def render_results(root: Path, case_id: str, hs_id: str) -> None:
    work = root / "WORK"
    hpr = work / f"{case_id}.HPR"
    hsr = work / f"{hs_id}.HSR"
    hsf = work / f"{hs_id}.HSF"

    hpr_df = parse_hpr(hpr) if hpr.exists() else pd.DataFrame()
    hsr_df = parse_hsr(hsr) if hsr.exists() else pd.DataFrame()
    hsf_df = parse_hsf(hsf) if hsf.exists() else pd.DataFrame()

    if hpr_df.empty and hsr_df.empty:
        st.info("아직 표시할 계산 결과가 없습니다.")
        return

    st.subheader("계산 결과")
    metric_cols = st.columns(4)
    if not hpr_df.empty:
        last = hpr_df.iloc[-1]
        metric_cols[0].metric("HFPLUME 전환거리", f"{last['distance_m']:.1f} m")
        metric_cols[1].metric("전환점 HF 농도", f"{last['hf_ppm']:,.0f} ppm")
        metric_cols[2].metric("전환점 플룸 직경", f"{last['effective_diameter_m']:.2f} m")
    if not hsr_df.empty:
        threshold = float(st.session_state.get("hegadas_threshold", 0.1))
        crossing = first_crossing_distance(hsr_df, "distance_m", "pollutant_vol_pct", threshold)
        metric_cols[3].metric(
            f"{threshold:g} vol% 도달거리",
            f"{crossing:.1f} m" if crossing is not None else "범위 밖",
        )

    tab1, tab2, tab3, tab4 = st.tabs(["농도", "플룸 형상", "HF 기체·포그", "원자료"])

    with tab1:
        fig = go.Figure()
        if not hpr_df.empty:
            fig.add_trace(go.Scatter(
                x=hpr_df["distance_m"], y=hpr_df["hf_ppm"], mode="lines+markers",
                name="HFPLUME HF",
                hovertemplate="거리 %{x:.1f} m<br>HF %{y:,.0f} ppm<extra></extra>",
            ))
        if not hsr_df.empty:
            fig.add_trace(go.Scatter(
                x=hsr_df["distance_m"], y=hsr_df["pollutant_ppm"], mode="lines+markers",
                name="HEGADAS-S pollutant",
                hovertemplate="거리 %{x:.1f} m<br>혼합물 %{y:,.0f} ppm<extra></extra>",
            ))
        fig.update_yaxes(type="log", title="농도 (ppm, 로그축)")
        fig.update_xaxes(title="누출원으로부터 거리 (m)")
        fig.update_layout(legend_title_text="모델/농도 정의", height=480)
        st.plotly_chart(fig, use_container_width=True)
        st.caption("HFPLUME의 HF 농도와 HEGADAS-S의 전체 pollutant 농도는 정의가 다르므로 하나의 동일 농도곡선으로 단정하지 않습니다.")

    with tab2:
        if not hpr_df.empty:
            shape_df = hpr_df[["distance_m", "plume_height_m", "effective_diameter_m"]].copy()
            shape_df["plume_radius_m"] = shape_df["effective_diameter_m"] / 2
            fig_height = px.line(shape_df, x="distance_m", y=["plume_height_m", "plume_radius_m"], markers=True)
            fig_height.update_layout(
                xaxis_title="거리 (m)", yaxis_title="높이/유효 반경 (m)",
                legend_title_text="변수", height=440,
            )
            st.plotly_chart(fig_height, use_container_width=True)
        if not hsr_df.empty:
            fig_boundary = px.line(
                hsr_df,
                x="distance_m",
                y=["y_lower_m", "z_lower_m"],
                markers=True,
            )
            fig_boundary.update_layout(
                xaxis_title="거리 (m)", yaxis_title="하위 농도 경계 규모 (m)",
                legend_title_text="경계 변수", height=440,
            )
            st.plotly_chart(fig_boundary, use_container_width=True)

    with tab3:
        if hsf_df.empty:
            st.info("HF-specific 결과(.HSF)가 생성되지 않았습니다.")
        else:
            phase_df = hsf_df[["distance_m", "hf_gas_pct_est", "hf_fog_pct_est"]].copy()
            phase_long = phase_df.melt(
                id_vars="distance_m", var_name="phase", value_name="estimated_pct"
            )
            labels = {
                "hf_gas_pct_est": "기체상 HF(단량체 환산 근사)",
                "hf_fog_pct_est": "포그상 HF(단량체 환산 근사)",
            }
            phase_long["phase"] = phase_long["phase"].map(labels)
            fig_phase = px.area(
                phase_long, x="distance_m", y="estimated_pct", color="phase",
                groupnorm=None,
            )
            fig_phase.update_layout(
                xaxis_title="거리 (m)", yaxis_title="HF 상 분배 추정치 (%)",
                legend_title_text="상", height=460,
            )
            st.plotly_chart(fig_phase, use_container_width=True)
            st.caption("Y11 + 2Y12 + 6Y16 + 8Y18 + YC와 XL×LFOG를 사용한 단량체 환산 파생값입니다. HGSYSTEM이 직접 출력한 kg/s 값은 아닙니다.")

    with tab4:
        if not hpr_df.empty:
            st.markdown("**HFPLUME 표**")
            st.dataframe(hpr_df, use_container_width=True, hide_index=True)
            st.download_button("HFPLUME CSV", dataframe_csv_bytes(hpr_df), f"{case_id}_HFPLUME.csv", "text/csv")
        if not hsr_df.empty:
            st.markdown("**HEGADAS-S 표**")
            st.dataframe(hsr_df, use_container_width=True, hide_index=True)
            st.download_button("HEGADAS-S CSV", dataframe_csv_bytes(hsr_df), f"{hs_id}_HEGADASS.csv", "text/csv")
        if not hsf_df.empty:
            st.markdown("**HF-specific 표**")
            st.dataframe(hsf_df, use_container_width=True, hide_index=True)
            st.download_button("HF-specific CSV", dataframe_csv_bytes(hsf_df), f"{hs_id}_HF_SPECIFIC.csv", "text/csv")

    output_paths = sorted(work.glob(f"{case_id}.*")) + sorted(work.glob(f"{hs_id}.*"))
    st.download_button(
        "이번 계산 결과 전체 ZIP",
        result_zip_bytes(output_paths),
        file_name=f"{case_id}_HGSYSTEM_results.zip",
        mime="application/zip",
    )


st.set_page_config(page_title="HGSYSTEM 로컬 실행기", page_icon="🧪", layout="wide")
st.title("HGSYSTEM 로컬 실행기")
st.caption("내 PC에서만 실행되는 HFPLUME → HEGADAS-S 간편 인터페이스")

with st.sidebar:
    st.header("설치 위치")
    root_text = st.text_input("HGSYSTEM 폴더", value=str(DEFAULT_HGSYSTEM_DIR))
    root = Path(root_text).expanduser().resolve()
    missing_files = validate_install(root)
    if missing_files:
        st.error("HGSYSTEM 핵심 파일을 찾지 못했습니다.")
        st.code("\n".join(missing_files), language="text")
    else:
        st.success("HGSYSTEM 설치 확인 완료")
    st.caption("권장 위치: C:\\HGSYSTEM (공백·한글 없는 경로)")

if os.name != "nt":
    st.warning("이 화면은 열 수 있지만 HGSYSTEM EXE/BAT 실행은 Windows에서만 가능합니다.")

if missing_files:
    st.stop()

work = root / "WORK"
work.mkdir(exist_ok=True)

st.info("이 앱은 인터넷에 올리지 않습니다. 실행 주소가 127.0.0.1이므로 같은 PC 사용자만 볼 수 있습니다.")

with st.form("hfplume_form"):
    top1, top2, top3 = st.columns(3)
    with top1:
        case_id_input = st.text_input("계산 이름(영문·숫자, 8자 이하)", value="CASE01")
    with top2:
        title = st.text_input("영문 사례 제목", value="HF release case from Streamlit")
    with top3:
        run_hegadas = st.checkbox("HFPLUME 후 HEGADAS-S 자동 실행", value=True)

    st.subheader("1. 저장용기와 조성")
    c1, c2, c3, c4, c5 = st.columns(5)
    tres = c1.number_input("저장온도 TRES (℃)", value=40.0, step=1.0)
    pres = c2.number_input("저장 절대압력 PRES (atm)", min_value=0.01, value=6.0, step=0.1)
    hfres = c3.number_input("HF 질량분율 (%)", min_value=0.0, max_value=100.0, value=80.0, step=1.0)
    hcres = c4.number_input("추가 이상기체 (%)", min_value=0.0, max_value=100.0, value=2.0, step=0.5)
    h2ores = c5.number_input("물 질량분율 (%)", min_value=0.0, max_value=100.0, value=2.0, step=0.5)

    with st.expander("추가 이상기체 물성"):
        g1, g2 = st.columns(2)
        cpgas = g1.number_input("CPGAS (J/mol/℃)", min_value=0.0, value=71.7, step=0.1)
        mmgas = g2.number_input("MMGAS (g/mol)", min_value=0.01, value=44.0, step=0.1)

    st.subheader("2. 누출 조건")
    c1, c2, c3, c4, c5 = st.columns(5)
    dmdt = c1.number_input("전체 누출량 DMDT (kg/s)", min_value=0.0001, value=3.0, step=0.1)
    dexit = c2.number_input("누출구 직경 DEXIT (m)", min_value=0.00001, value=0.04191, format="%.5f")
    zexit = c3.number_input("누출 높이 ZEXIT (m)", min_value=0.0, value=1.263, step=0.1)
    angle = c4.number_input("누출 각도 ANGLE (°)", min_value=-90.0, max_value=90.0, value=0.0, step=5.0)
    duration = c5.number_input("누출시간 DURATION (s, -1=정상상태)", value=-1.0, step=60.0)

    st.subheader("3. 기상과 지표")
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    z0 = c1.number_input("풍속 기준높이 Z0 (m)", min_value=0.01, value=2.0, step=0.5)
    u0 = c2.number_input("풍속 U0 (m/s)", min_value=0.01, value=5.6, step=0.1)
    airtemp = c3.number_input("대기온도 (℃)", value=20.0, step=1.0)
    airpress = c4.number_input("대기압 (atm)", min_value=0.1, value=1.0, step=0.01)
    rhperc = c5.number_input("상대습도 (%)", min_value=0.0, max_value=100.0, value=70.0, step=1.0)
    zr = c6.number_input("지표 거칠기 ZR (m)", min_value=0.00001, value=0.003, format="%.5f")
    pqstab = st.selectbox("Pasquill 안정도", options=list("ABCDEF"), index=3)

    st.subheader("4. 계산 종료와 전환")
    c1, c2, c3, c4 = st.columns(4)
    xlst = c1.number_input("HFPLUME 최대거리 XLST (m)", min_value=1.0, value=1000.0, step=100.0)
    hflst = c2.number_input("HFPLUME 종료 HF 농도 HFLST (mole%)", min_value=0.000001, value=0.01, format="%.6f")
    hs_comin = c3.number_input("HEGADAS-S 종료농도 COMIN (vol%)", min_value=0.000001, value=0.1, format="%.6f")
    hs_cuv = c4.number_input("HEGADAS-S 상위 경계 CUV (vol%)", min_value=0.000001, value=2.0, format="%.6f")
    hs_clv = st.number_input("HEGADAS-S 하위 경계 CLV (vol%)", min_value=0.000001, value=0.1, format="%.6f")

    with st.expander("고급 MATCH 기준(기본값 유지 권장)"):
        m1, m2, m3, m4, m5 = st.columns(5)
        rulst = m1.number_input("RULST", value=0.1, format="%.4f")
        relst = m2.number_input("RELST", value=0.3, format="%.4f")
        rglst = m3.number_input("RGLST", value=0.3, format="%.4f")
        rnlst = m4.number_input("RNLST", value=0.1, format="%.4f")
        ralst = m5.number_input("RALST", value=0.2, format="%.4f")

    submitted = st.form_submit_button("계산 실행", type="primary", use_container_width=True)

if submitted:
    errors: list[str] = []
    if hfres + hcres + h2ores > 99.0:
        errors.append("HF·추가가스·물의 합은 건조공기 1% 이상을 남기도록 99% 이하를 권장합니다.")
    if duration == 0:
        errors.append("DURATION은 정상상태일 때 -1, 유한 누출일 때 0보다 큰 값을 사용하세요.")
    if hs_clv > hs_cuv:
        errors.append("HEGADAS-S 하위 경계 CLV는 상위 경계 CUV보다 작거나 같아야 합니다.")

    if errors:
        for error in errors:
            st.error(error)
    else:
        case_id = sanitize_case_id(case_id_input)
        hs_id = sanitize_case_id((case_id[:7] + "S") if len(case_id) >= 8 else (case_id + "S"))
        values = {
            "TRES": tres, "PRES": pres, "HFRES": hfres, "HCRES": hcres, "H2ORES": h2ores,
            "CPGAS": cpgas, "MMGAS": mmgas, "DMDT": dmdt, "DEXIT": dexit,
            "ZEXIT": zexit, "ANGLE": angle, "DURATION": duration, "Z0": z0, "U0": u0,
            "AIRTEMP": airtemp, "AIRPRESS": airpress, "RHPERC": rhperc, "ZR": zr,
            "PQSTAB": pqstab, "XLST": xlst, "HFLST": hflst, "HCLST": -1.0,
            "RULST": rulst, "RELST": relst, "RGLST": rglst, "RNLST": rnlst, "RALST": ralst,
        }

        template = root / "STINPUT" / "SKELETON.HPI"
        hpi = work / f"{case_id}.HPI"
        missing_keys = build_hpi(template, hpi, title, values)
        if missing_keys:
            st.warning("템플릿에서 일부 항목을 찾지 못했습니다: " + ", ".join(missing_keys))

        try:
            with st.spinner("HFPLUME 계산 중..."):
                hp_result = run_batch(root, work, "HFPLUME.BAT", [case_id])
            st.session_state["hp_result"] = hp_result
            st.session_state["case_id"] = case_id
            st.session_state["hs_id"] = hs_id
            st.session_state["hegadas_threshold"] = hs_comin

            hpr_path = work / f"{case_id}.HPR"
            hpe_path = work / f"{case_id}.HPE"
            if hpe_path.exists():
                st.error("HFPLUME 오류 파일이 생성되었습니다.")
                st.code(read_text_flexible(hpe_path), language="text")
            elif not hpr_path.exists():
                st.error("HFPLUME 결과 HPR 파일이 생성되지 않았습니다. 실행 기록을 확인하세요.")
            else:
                st.success("HFPLUME 계산 완료")

            if run_hegadas and (work / f"{case_id}.HSL").exists():
                hsl = work / f"{case_id}.HSL"
                missing_hsl = patch_hsl(hsl, hs_comin, hs_clv, hs_cuv)
                if missing_hsl:
                    st.warning("생성된 HSL에서 다음 항목을 자동 수정하지 못했습니다: " + ", ".join(missing_hsl))
                with st.spinner("HEGADAS-S 계산 중..."):
                    hs_result = run_batch(root, work, "HEGADASS.BAT", [hsl.name, hs_id])
                st.session_state["hs_result"] = hs_result
                hse_path = work / f"{hs_id}.HSE"
                hsr_path = work / f"{hs_id}.HSR"
                if hse_path.exists():
                    st.error("HEGADAS-S 오류 파일이 생성되었습니다.")
                    st.code(read_text_flexible(hse_path), language="text")
                elif hsr_path.exists():
                    st.success("HEGADAS-S 계산 완료")
                else:
                    st.error("HEGADAS-S 결과 HSR 파일이 생성되지 않았습니다. 실행 기록을 확인하세요.")
        except subprocess.TimeoutExpired:
            st.error("계산 시간이 제한을 초과했습니다.")
        except Exception as exc:
            st.exception(exc)

case_id = st.session_state.get("case_id")
hs_id = st.session_state.get("hs_id")
render_log("HFPLUME 실행 기록", st.session_state.get("hp_result"))
render_log("HEGADAS-S 실행 기록", st.session_state.get("hs_result"))
if case_id and hs_id:
    render_results(root, case_id, hs_id)
else:
    st.subheader("기존 계산 결과 열기")
    hpr_files = sorted(work.glob("*.HPR"), key=lambda p: p.stat().st_mtime, reverse=True)
    if hpr_files:
        selected = st.selectbox("HFPLUME 결과", hpr_files, format_func=lambda p: p.name)
        selected_case = selected.stem[:8]
        hs_candidates = sorted(work.glob("*.HSR"), key=lambda p: p.stat().st_mtime, reverse=True)
        selected_hs = st.selectbox(
            "HEGADAS-S 결과",
            [None] + hs_candidates,
            format_func=lambda p: "선택 안 함" if p is None else p.name,
        )
        if st.button("기존 결과 표시"):
            render_results(root, selected_case, selected_hs.stem if selected_hs else "__NONE__")
    else:
        st.info("입력값을 확인한 뒤 계산 실행을 누르세요.")

st.divider()
st.caption("연구·검토용 도구입니다. 규제 제출이나 비상대응 판단 전에는 원본 HGSYSTEM 보고서, 경고·오류 파일과 질량수지를 함께 검토하세요.")
