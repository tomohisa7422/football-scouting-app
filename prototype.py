import streamlit as st
import pdfplumber
import pandas as pd
import plotly.express as px
import re
import io

# ReportLab関連のインポート（日本語PDF + グラフ描画用）
from reportlab.lib.pagesizes import A4, landscape
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.graphics.shapes import Drawing
from reportlab.graphics.charts.piecharts import Pie
from reportlab.graphics.charts.legends import Legend

# ==========================================
# 1. アプリの基本設定
# ==========================================
st.set_page_config(page_title="Football Scouting AI", layout="wide")
st.title("🏈 アメフト 試合スタッツ自動集計アプリ")
st.markdown("PDFから抽出したデータを**自由に変形・修正・時間フィルター・円グラフ付きPDF出力**できます。")

def time_to_seconds(time_str):
    try:
        parts = time_str.split(':')
        if len(parts) == 2: return int(parts[0]) * 60 + int(parts[1])
    except: pass
    return 0

def seconds_to_time(seconds):
    mins = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{mins:02d}:{secs:02d}"

# ==========================================
# 2. PDF自動解析ロジック（重複防止版）
# ==========================================
def parse_football_pdf_fully(pdf_file):
    play_data, stats_data_list = [], []
    current_qtr, current_pos_team, current_time_str = "1Q", "UNKNOWN", "12:00"
    
    with pdfplumber.open(pdf_file) as pdf:
        for page in pdf.pages[1:]:
            text = page.extract_text()
            if not text: continue
            lines = text.split("\n")
            for i, line in enumerate(lines):
                line_str = line.strip()
                if "First Quarter" in line or "1st Quarter" in line: current_qtr = "1Q"
                elif "Second Quarter" in line or "2Q" in line: current_qtr = "2Q"
                elif "Third Quarter" in line or "3Q" in line: current_qtr = "3Q"
                elif "Fourth Quarter" in line or "4Q" in line: current_qtr = "4Q"
                
                team_turn_match = re.search(r'^([^\d\s]+(?:\s+[^\d\s]+)*)\s+(\d{1,2}:\d{2})', line_str)
                if team_turn_match:
                    team_name_candidate = team_turn_match.group(1).strip()
                    if not any(k in team_name_candidate.upper() for k in ["PLAY BY PLAY", "END OF", "SCORE", "TEAM"]):
                        current_pos_team = team_name_candidate
                        current_time_str = team_turn_match.group(2).strip()
                
                play_match = re.search(r'(\d)\s*&\s*(\d+|G|Goal)\s*-\s*([A-Z]{2})\s*(\d+)\s*([LMR])\s*(RUN|PASS|PUNT|FG)', line_str)
                if play_match:
                    dist_raw = play_match.group(2).strip().upper()
                    yardline = int(play_match.group(4))
                    distance = str(yardline) if dist_raw in ["G", "GOAL"] else dist_raw
                    
                    play_data.append({
                        "Qtr": current_qtr, "Time": current_time_str, "TimeSec": time_to_seconds(current_time_str),
                        "Down": int(play_match.group(1)), "Distance": distance,
                        "PosTeam": current_pos_team, "FieldSide": play_match.group(3),
                        "Yardline": yardline, "Hash": play_match.group(5),
                        "PlayType": play_match.group(6), "RawText": line_str
                    })
                
                if "END OF QUARTER" in line_str or "END OF GAME" in line_str or "QUARTER" in line_str.upper():
                    scan_idx = i + 1
                    while scan_idx < min(i + 15, len(lines)):
                        clean_line = lines[scan_idx].replace('"', '').replace(',', ' ')
                        sm = re.search(r'([^\d\s]+(?:\s+[^\d\s]+)*)\s+(\d+)\s+(\d+:\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+)\s+(\d+/\d+)\s+(\d+/\d+)', clean_line)
                        if sm:
                            team_name = sm.group(1).strip()
                            if "チーム名" not in team_name and "SCORE" not in team_name.upper():
                                is_duplicate = any(d["Qtr"] == current_qtr and d["チーム名"] == team_name for d in stats_data_list)
                                if not is_duplicate:
                                    # 💡 全ての項目（1stDown内訳、サードダウン成功率など）を漏れなく保存
                                    stats_data_list.append({
                                        "Qtr": current_qtr, 
                                        "チーム名": team_name, 
                                        "得点 (Score)": int(sm.group(2)),
                                        "攻撃時間 (Time Poss)": sm.group(3), 
                                        "攻撃秒数": time_to_seconds(sm.group(3)),
                                        "1stDown(ラン)": int(sm.group(4)),
                                        "1stDown(パス)": int(sm.group(5)),
                                        "1stDown(反則)": int(sm.group(6)),
                                        "1stDown(合計)": int(sm.group(7)),
                                        "3rdDown成功率": sm.group(8).strip(),
                                        "4thDown成功率": sm.group(9).strip()
                                    })
                        scan_idx += 1
                        
    df_plays_out = pd.DataFrame(play_data)
    df_stats_out = pd.DataFrame(stats_data_list)
    if not df_stats_out.empty:
        df_stats_out = df_stats_out.drop_duplicates(subset=["Qtr", "チーム名"], keep="first")
        
    return df_plays_out, df_stats_out

# ==========================================
# 4. 日本語PDF生成関数
# ==========================================
def generate_scouting_pdf_with_chart(filtered_df, team_name, criteria_text, run_count, pass_count):
    try:
        pdfmetrics.registerFont(TTFont('MSGothic', 'C:\\Windows\\Fonts\\msgothic.ttc'))
        font_name = 'MSGothic'
    except:
        font_name = 'Helvetica'

    total = run_count + pass_count
    run_ratio = (run_count / total * 100) if total > 0 else 0
    pass_ratio = (pass_count / total * 100) if total > 0 else 0

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=landscape(A4), rightMargin=30, leftMargin=30, topMargin=30, bottomMargin=30)
    story = []

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('TitleStyle', parent=styles['Heading1'], fontName=font_name, fontSize=20, textColor=colors.HexColor('#1e3a8a'))
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontName=font_name, fontSize=10, textColor=colors.HexColor('#475569'), spaceAfter=10)

    story.append(Paragraph("🏈 Football Scouting AI - スカウティングレポート", title_style))
    story.append(Paragraph(f"<b>対象チーム:</b> {team_name} &nbsp;&nbsp;|&nbsp;&nbsp; <b>条件:</b> {criteria_text}", meta_style))
    story.append(Spacer(1, 10))

    if total > 0:
        d = Drawing(400, 150)
        pc = Pie()
        pc.x = 100
        pc.y = 15
        pc.width = 120
        pc.height = 120
        pc.data = [run_count, pass_count]
        pc.labels = [f'RUN: {run_ratio:.1f}% ({run_count}回)', f'PASS: {pass_ratio:.1f}% ({pass_count}回)']
        pc.sideLabels = True
        pc.slices.fontName = font_name
        pc.slices.fontSize = 9
        pc.slices[0].fillColor = colors.HexColor('#1e3a8a')
        pc.slices[1].fillColor = colors.HexColor('#60a5fa')
        
        leg = Legend()
        leg.x = 280
        leg.y = 80
        leg.fontName = font_name
        leg.fontSize = 10
        leg.colorNamePairs = [
            (colors.HexColor('#1e3a8a'), f'RUN: {run_ratio:.1f}% ({run_count}回)'), 
            (colors.HexColor('#60a5fa'), f'PASS: {pass_ratio:.1f}% ({pass_count}回)')
        ]
        
        d.add(pc)
        d.add(leg)
        story.append(d)
        story.append(Spacer(1, 15))

    header_style = ParagraphStyle('HeaderStyle', parent=styles['Normal'], fontName=font_name, fontSize=9, textColor=colors.white)
    cell_style = ParagraphStyle('CellStyle', parent=styles['Normal'], fontName=font_name, fontSize=8, leading=10)
    
    table_data = [[Paragraph(f"<b>{h}</b>", header_style) for h in ["Qtr", "Time", "Down", "Dist", "Side", "Yard", "Hash", "Type", "公式テキスト内容"]]]

    for _, row in filtered_df.iterrows():
        p_color = "#1e3a8a" if row['PlayType'] == "RUN" else "#2563eb"
        type_style = ParagraphStyle('TypeStyle', parent=cell_style, textColor=colors.HexColor(p_color), fontName=font_name)
        
        table_data.append([
            Paragraph(str(row['Qtr']), cell_style), Paragraph(str(row['Time']), cell_style),
            Paragraph(str(row['Down']), cell_style), Paragraph(str(row['Distance']), cell_style),
            Paragraph(str(row['FieldSide']), cell_style), Paragraph(str(row['Yardline']), cell_style),
            Paragraph(str(row['Hash']), cell_style), Paragraph(f"<b>{row['PlayType']}</b>", type_style),
            Paragraph(str(row['RawText']), cell_style)
        ])

    play_table = Table(table_data, colWidths=[30, 40, 30, 30, 40, 30, 30, 40, 510], repeatRows=1)
    play_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#334155')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    story.append(play_table)

    doc.build(story)
    return buffer.getvalue()

# ==========================================
# 5. 画面表示
# ==========================================
uploaded_file = st.file_uploader("PDFファイルをここにドロップしてください", type="pdf")

if uploaded_file:
    if "last_file" not in st.session_state or st.session_state.last_file != uploaded_file.name:
        dfp, dfs = parse_football_pdf_fully(uploaded_file)
        st.session_state.df_plays, st.session_state.df_stats = dfp, dfs
        st.session_state.last_file = uploaded_file.name
        unique_teams = sorted(list(dfp["PosTeam"].unique())) if not dfp.empty else []
        st.session_state.team_mapping = {t: ("BA" if "バーバリアン" in t else "IG" if "ゴリラ" in t else t[:2].upper()) for t in unique_teams}

    st.info("💡 敵陣・自陣判定のため、各チームの略称を確認・修正してください。")
    map_cols = st.columns(max(len(st.session_state.team_mapping), 2))
    for idx, (t_name, old_abbr) in enumerate(st.session_state.team_mapping.items()):
        with map_cols[idx]:
            st.session_state.team_mapping[t_name] = st.text_input(f"「{t_name}」の略称", value=old_abbr, max_chars=2, key=f"abbr_{t_name}").upper()

    tab1, tab2 = st.tabs(["📈プレイ集計", "📊 チームスタッツ"])
    
    with tab1:
        st.header("🏈 プレイライブラリ（直接編集可能）")
        
        cols_order = ["Qtr", "Time", "Down", "Distance", "PosTeam", "FieldSide", "Yardline", "Hash", "PlayType", "RawText", "TimeSec"]
        df_plays_ordered = st.session_state.df_plays[cols_order] if not st.session_state.df_plays.empty and "Time" in st.session_state.df_plays.columns else st.session_state.df_plays
        
        df_plays = st.data_editor(df_plays_ordered, num_rows="dynamic", use_container_width=True, key="editor",
                                  column_config={"TimeSec": None, "Qtr": st.column_config.SelectboxColumn("Qtr", options=["1Q", "2Q", "3Q", "4Q"]), "PlayType": st.column_config.SelectboxColumn("PlayType", options=["RUN", "PASS", "PUNT", "FG"])})
        
        if not df_plays.empty and "Time" in df_plays.columns:
            df_plays["TimeSec"] = df_plays["Time"].apply(time_to_seconds)
        st.session_state.df_plays = df_plays

        st.subheader("💾 編集したデータをエクスポート")
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine='openpyxl') as writer:
            save_df = df_plays.drop(columns=["TimeSec"]) if "TimeSec" in df_plays.columns else df_plays
            save_df.to_excel(writer, index=False, sheet_name='PlaybyPlay')
        
        col_dl1, col_dl2 = st.columns(2)
        with col_dl1:
            st.download_button(label="📁 Excelファイル(.xlsx)として保存", data=buffer.getvalue(), file_name="scouting_data_edited.xlsx")
        with col_dl2:
            st.download_button(label="📄 CSVファイル(.csv)として保存", data=save_df.to_csv(index=False).encode('utf-8-sig'), file_name="scouting_data_edited.csv")

        st.divider()
        
        # ==========================================
        # フィルターコントロール（左サイドバー）- タブの外に配置
        # ==========================================
        st.sidebar.header("🔍データ絞り込みフィルター")
        
        team_options = sorted(list(df_plays["PosTeam"].unique())) if "PosTeam" in df_plays.columns else []
        selected_team = st.sidebar.selectbox("分析対象の攻撃チーム", options=team_options)
        
        # 💡 ここで選択された qtr_filter が、プレイ分析とチームスタッツの両方に連動します
        qtr_options = ["1Q", "2Q", "3Q", "4Q"]
        qtr_filter = st.sidebar.multiselect("Qtr (クォーター選択)", options=qtr_options, default=qtr_options)
        
        down_options = sorted(list(df_plays["Down"].unique())) if "Down" in df_plays.columns else [1, 2, 3, 4]
        down_filter = st.sidebar.multiselect("Down", options=down_options, default=down_options)
        
        hash_options = list(df_plays["Hash"].unique()) if "Hash" in df_plays.columns else ["L", "M", "R"]
        hash_filter = st.sidebar.multiselect("Hash", options=hash_options, default=hash_options)
        
        pt_options = [opt for opt in list(df_plays["PlayType"].unique()) if opt in ["RUN", "PASS", "PUNT", "FG"]]
        if not pt_options: pt_options = ["RUN", "PASS"]
        pt_default = [opt for opt in pt_options if opt in ["RUN", "PASS"]]
        play_type_filter = st.sidebar.multiselect("PlayType", options=pt_options, default=pt_default)
        
        # --- 残りヤード数（Distance）セクション ---
        st.sidebar.markdown("---")
        st.sidebar.markdown("**📐 残りヤード数（Distance）**")
        
        chk_all = st.sidebar.checkbox("すべて", value=True)
        chk_short = st.sidebar.checkbox("ショート (1-3yd)", value=False)
        chk_mid = st.sidebar.checkbox("ミドル (4-6yd)", value=False)
        chk_long = st.sidebar.checkbox("ロング (7-10yd)", value=False)
        chk_vlong = st.sidebar.checkbox("超ロング (11yd以上)", value=False)
        
        st.sidebar.markdown("⬇️ **個別設定数値フィルター**")
        use_custom_slider = st.sidebar.checkbox("個別設定を有効にする", value=False)
        dist_slider_range = st.sidebar.slider("ヤード範囲(yd)", min_value=1, max_value=30, value=(1, 30), disabled=not use_custom_slider)
        
        if use_custom_slider:
            if dist_slider_range[1] == 30:
                st.sidebar.caption(f"🎯 現在： **{dist_slider_range[0]}yd 〜 30yd以上すべて** を抽出中")
            else:
                st.sidebar.caption(f"🎯 現在： **{dist_slider_range[0]}yd 〜 {dist_slider_range[1]}yd** を抽出中")
        else:
            st.sidebar.caption("💡 個別設定を有効にすると、上記スライダーで細かく絞り込めます。")

        st.sidebar.markdown("---")
        st.sidebar.markdown("**⏱️ クォーター内の残り時間（Time）**")
        max_sec = int(df_plays["TimeSec"].max()) if not df_plays.empty and df_plays["TimeSec"].max() > 0 else 720
        selected_time_range = st.sidebar.slider("残り時間範囲", min_value=0, max_value=max_sec if max_sec > 0 else 720, value=(0, max_sec if max_sec > 0 else 720))
        st.sidebar.caption(f"選択： **{seconds_to_time(selected_time_range[0])}** 〜 **{seconds_to_time(selected_time_range[1])}**")

        st.sidebar.markdown("**📍 敵陣ゴールまでの総距離**")
        field_pos_range = st.sidebar.slider("敵陣ゴールまでの距離(yd)", min_value=1, max_value=100, value=(1, 100))

        # ==========================================
        # フィルターロジックの実行
        # ==========================================
        def calculate_dist_to_goal_dynamic(row):
            try:
                yd_line = int(row["Yardline"])
                pos_team_str = str(row["PosTeam"])
                field_side_str = str(row["FieldSide"]).upper().strip()
                mapped_abbr = st.session_state.team_mapping.get(pos_team_str, "??").upper().strip()
                if field_side_str == mapped_abbr: return 100 - yd_line
                else: return yd_line
            except: return 50

        if not df_plays.empty: df_plays["_DistToGoal"] = df_plays.apply(calculate_dist_to_goal_dynamic, axis=1)
        else: df_plays["_DistToGoal"] = 50

        # 基本フィルターマスク
        mask = (
            (df_plays["Qtr"].isin(qtr_filter)) & 
            (df_plays["Down"].isin(down_filter)) & 
            (df_plays["Hash"].isin(hash_filter)) &
            (df_plays["PlayType"].isin(play_type_filter)) &
            (df_plays["TimeSec"].between(selected_time_range[0], selected_time_range[1])) & 
            (df_plays["_DistToGoal"].between(field_pos_range[0], field_pos_range[1]))
        )
        if "PosTeam" in df_plays.columns: mask = mask & (df_plays["PosTeam"] == selected_team)

        def get_int_value(x):
            try: return int(x)
            except: return None
        temp_dists = df_plays["Distance"].apply(get_int_value)

        if use_custom_slider:
            if dist_slider_range[1] == 30:
                mask = mask & ((temp_dists >= dist_slider_range[0]) | temp_dists.isna())
            else:
                mask = mask & (temp_dists.between(dist_slider_range[0], dist_slider_range[1]) | temp_dists.isna())

        if not chk_all:
            yard_masks = []
            if chk_short: yard_masks.append(temp_dists.between(1, 3))
            if chk_mid: yard_masks.append(temp_dists.between(4, 6))
            if chk_long: yard_masks.append(temp_dists.between(7, 10))
            if chk_vlong: yard_masks.append(temp_dists >= 11)
            
            if yard_masks:
                final_yard_mask = yard_masks[0]
                for m in yard_masks[1:]: final_yard_mask = final_yard_mask | m
                mask = mask & final_yard_mask

        filtered_df = df_plays[mask]
        analysis_df = filtered_df[filtered_df["PlayType"].isin(["RUN", "PASS"])]
        run_count = int((analysis_df["PlayType"] == "RUN").sum())
        pass_count = int((analysis_df["PlayType"] == "PASS").sum())

        # ==========================================
        # メイン画面（右側）への描画
        # ==========================================
        st.header("📊 RUN/PASS傾向分析")
        
        if not filtered_df.empty:
            res_c1, res_c2 = st.columns([1, 2])
            with res_c1:
                if not analysis_df.empty:
                   fig = px.pie(
                        analysis_df, 
                        names="PlayType", 
                        title=f"{selected_team} RUN vs PASS 比率", 
                        color="PlayType", 
                        color_discrete_map={"RUN": "#1e3a8a", "PASS": "#60a5fa"},
                        category_orders={"PlayType": ["RUN", "PASS"]}
                    )
                   fig.update_traces(
                        textinfo='percent+value', 
                        hovertemplate="<b>%{label}</b><br>割合: %{percent}<br>回数: %{value}回<extra></extra>"
                    )
                   st.plotly_chart(fig, use_container_width=True)
            with res_c2:
                st.subheader("抽出プレイ詳細")
                st.dataframe(filtered_df[["Qtr", "Time", "Down", "Distance", "FieldSide", "Yardline", "Hash", "PlayType", "RawText"]], use_container_width=True)
            
            st.divider()
            st.subheader("🖨️ スカウティングレポート(PDF)の保存")
            
            if use_custom_slider:
                dist_txt = f"{dist_slider_range[0]}-30+yd" if dist_slider_range[1] == 30 else f"{dist_slider_range[0]}-{dist_slider_range[1]}yd"
            else:
                dist_txt = "すべて" if chk_all else "プリセット選択"
                
            crit = f"Qtr: {qtr_filter} | Down: {down_filter} | Dist: {dist_txt}"
            
            pdf_bytes = generate_scouting_pdf_with_chart(filtered_df, selected_team, crit, run_count, pass_count)
            st.download_button(label="📄 円グラフ付きPDFレポートをダウンロード", data=pdf_bytes, file_name=f"report_{selected_team}.pdf", mime="application/pdf")
        else:
            st.warning("左側のサイドバーで選択された条件に該当するデータがありません。条件を緩めてください。")

    with tab2:
        st.header("📊 チームスタッツ集計（Qtr選択のフィルターのみ反映）")
        
        if not st.session_state.df_stats.empty:
            raw_stats = st.session_state.df_stats.copy()
            raw_stats.columns = raw_stats.columns.str.strip()
            
            # 元データの段階での重複を確実に排除
            unique_raw_stats = raw_stats.drop_duplicates(subset=["Qtr", "チーム名"], keep="first")
            # サイドバーの qtr_filter と連動
            filtered_stats = unique_raw_stats[unique_raw_stats["Qtr"].isin(qtr_filter)].copy()
            
            if not filtered_stats.empty:
                aggregated_data = []
                for team_name, group in filtered_stats.groupby("チーム名"):
                    
                    def get_safe_sum(col_name):
                        if col_name in group.columns:
                            return pd.to_numeric(group[col_name], errors='coerce').fillna(0).sum()
                        return 0
                    
                    score_sum = int(get_safe_sum("得点 (Score)"))
                    sec_sum = int(get_safe_sum("攻撃秒数"))
                    fd_run = int(get_safe_sum("1stDown(ラン)"))
                    fd_pass = int(get_safe_sum("1stDown(パス)"))
                    fd_pen = int(get_safe_sum("1stDown(反則)"))
                    fd_tot = int(get_safe_sum("1stDown(合計)"))
                    
                    # 計算値が元の合計を上回る場合の安全弁
                    if (fd_run + fd_pass + fd_pen) > fd_tot:
                        fd_tot = fd_run + fd_pass + fd_pen
                    
                    pos_time_str = f"{int(sec_sum // 60)}:{int(sec_sum % 60):02d}"
                    
                    def sum_fraction_column(col_name):
                        succ_total, att_total = 0, 0
                        if col_name in group.columns:
                            for val in group[col_name].dropna().astype(str):
                                if '/' not in val: continue
                                match = re.search(r'(\d+)\s*/\s*(\d+)', val)
                                if match:
                                    succ_total += int(match.group(1))
                                    att_total += int(match.group(2))
                        return f"{succ_total}/{att_total}"
                    
                    third_down_str = sum_fraction_column("3rdDown成功率")
                    fourth_down_str = sum_fraction_column("4thDown成功率")
                    
                    qtrs_included = ", ".join(sorted(list(group["Qtr"].unique())))
                    
                    aggregated_data.append({
                        "選択されたQtr": qtrs_included,
                        "チーム名": team_name,
                        "得点 (Score)": score_sum,
                        "攻撃時間 (Time Poss)": pos_time_str,
                        "攻撃秒数": sec_sum,
                        "1stDown(ラン)": fd_run,
                        "1stDown(パス)": fd_pass,
                        "1stDown(反則)": fd_pen,
                        "1stDown(合計)": fd_tot,
                        "3rdDown成功率": third_down_str,
                        "4thDown成功率": fourth_down_str
                    })
                
                df_aggregated = pd.DataFrame(aggregated_data)
                display_cols = ["選択されたQtr", "得点 (Score)", "攻撃時間 (Time Poss)", "攻撃秒数", 
                                "1stDown(ラン)", "1stDown(パス)", "1stDown(反則)", "1stDown(合計)", 
                                "3rdDown成功率", "4thDown成功率"]
                
                st.table(df_aggregated[["チーム名"] + display_cols].set_index("チーム名"))
            else:
                st.warning("選択されたQtrに該当するデータがありません。")
        else:
            st.info("PDFファイルをドロップして解析を開始してください。")
