"""
GreenData SQL Аналитик — Streamlit UI.

Запуск:
    .venv/bin/streamlit run app.py
"""

from __future__ import annotations

import html as _html
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from statistics import mean

import streamlit as st
from dotenv import load_dotenv

load_dotenv(override=True)

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

RESULTS_PATH = ROOT / "validation" / "results.json"

import os

from db.history import (
    save_query,
    load_history as db_load_history,
    clear_history as db_clear_history,
    get_tokens_used_today,
)

CEREBRAS_DAILY_LIMIT = int(os.getenv("CEREBRAS_DAILY_LIMIT", "1000000"))


def _load_history() -> list:
    try:
        rows = db_load_history(limit=200)
        # load_history returns newest-first; chat tab expects oldest-first
        return list(reversed(rows))
    except Exception:
        return []


def _save_history(history: list) -> None:
    pass  # save_query() called per-item; no batch write needed


def _save_record(record: dict) -> None:
    try:
        save_query(record)
    except Exception:
        pass

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="GreenData · SQL Аналитик",
    page_icon="🌿",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ── CSS ───────────────────────────────────────────────────────────────────────

st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');

body, p, div, span, a, li, td, th, label,
input, textarea, select, button,
h1, h2, h3, h4, h5, h6,
.stMarkdown, .stText, .stCode,
[data-testid="stChatInput"],
[data-testid="stChatMessage"] {
    font-family: 'Inter', sans-serif !important;
}
/* Возвращаем иконочный шрифт Streamlit (иначе стрелки экспандера рендерятся как текст) */
[class*="Icon"], [data-testid="Icon"],
span[class*="material"], .material-icons,
[data-testid="stExpander"] summary svg,
[data-baseweb="icon"] { font-family: initial !important; }

#MainMenu, footer, .stDeployButton { display: none !important; }
[data-testid="stToolbar"] { display: none !important; }
[data-testid="stHeader"] { display: none !important; }
[data-testid="stTop"] { display: none !important; }

.stApp { background: #F5F7FA; }
.block-container { padding-top: 1.2rem !important; }

/* ── Header ── */
.gd-topbar {
    background: white;
    padding: 14px 24px;
    border-radius: 16px;
    border: 1px solid #E8ECF0;
    margin-bottom: 18px;
    display: flex;
    align-items: center;
    gap: 14px;
    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
}
.gd-logo {
    width: 42px; height: 42px;
    background: linear-gradient(135deg, #3DC47A, #1BAD8E);
    border-radius: 12px;
    display: flex; align-items: center; justify-content: center;
    font-size: 22px; flex-shrink: 0;
}

/* ── Tabs ── */
.stTabs [data-baseweb="tab-list"] {
    background: transparent;
    border-bottom: 1px solid #E8ECF0;
    gap: 4px;
    margin-bottom: 8px;
}
.stTabs [data-baseweb="tab"] {
    color: #7B8794;
    font-weight: 500;
    padding: 10px 20px;
    font-size: 14px;
}
.stTabs [aria-selected="true"] {
    color: #3DC47A !important;
    border-bottom: 2px solid #3DC47A !important;
    background: transparent !important;
}

/* ── Chat input ── */
[data-testid="stChatInput"] textarea {
    border-radius: 14px !important;
    border: 1.5px solid #E8ECF0 !important;
    font-size: 15px !important;
    background: white !important;
}
[data-testid="stChatInput"] textarea:focus {
    border-color: #3DC47A !important;
    box-shadow: 0 0 0 3px rgba(61,196,122,0.12) !important;
}
[data-testid="stChatInput"] button {
    background: linear-gradient(135deg, #3DC47A, #1BAD8E) !important;
    border-radius: 10px !important;
    border: none !important;
}

/* ── Metric row ── */
.metric-row {
    display: flex;
    gap: 10px;
    margin: 14px 0 10px 0;
    flex-wrap: wrap;
    align-items: center;
}
.metric-box {
    background: #F5F7FA;
    border: 1px solid #E8ECF0;
    border-radius: 10px;
    padding: 9px 14px;
    font-size: 13px;
    color: #7B8794;
    line-height: 1.4;
}
.metric-box strong {
    color: #1A1A2E;
    font-size: 15px;
    display: block;
    margin-bottom: 1px;
}

/* ── Risk badges ── */
.badge-low    { background:#ECFDF5; color:#059669; border:1px solid #A7F3D0; }
.badge-medium { background:#FFFBEB; color:#D97706; border:1px solid #FDE68A; }
.badge-high   { background:#FEF2F2; color:#DC2626; border:1px solid #FECACA; }
.badge-low, .badge-medium, .badge-high {
    border-radius: 8px;
    padding: 7px 13px;
    font-size: 13px;
    font-weight: 600;
    display: inline-block;
}

/* ── Welcome cards ── */
.hint-card {
    background: white;
    border: 1px solid #E8ECF0;
    border-radius: 12px;
    padding: 10px 16px;
    font-size: 13px;
    color: #1A1A2E;
    cursor: pointer;
    display: inline-block;
}

/* ── Dashboard cards ── */
.dash-card {
    background: white;
    border-radius: 16px;
    padding: 22px 20px;
    border: 1px solid #E8ECF0;
    box-shadow: 0 2px 8px rgba(0,0,0,0.04);
    text-align: center;
    height: 100%;
}
.dash-value {
    font-size: 34px;
    font-weight: 700;
    color: #3DC47A;
    line-height: 1.2;
}
.dash-label {
    font-size: 13px;
    color: #7B8794;
    margin-top: 4px;
}
.dash-hint {
    font-size: 11px;
    color: #B0B8C1;
    margin-top: 6px;
}

/* ── Result list items ── */
.result-item {
    background: white;
    border: 1px solid #E8ECF0;
    border-radius: 12px;
    padding: 12px 16px;
    margin-bottom: 8px;
}

/* ── Button ── */
.stButton > button {
    background: linear-gradient(135deg, #3DC47A, #1BAD8E) !important;
    color: white !important;
    border: none !important;
    border-radius: 12px !important;
    font-weight: 600 !important;
    font-size: 14px !important;
    padding: 10px 20px !important;
}
</style>
""", unsafe_allow_html=True)

# ── Header ────────────────────────────────────────────────────────────────────

col_logo, col_reset = st.columns([8, 1])
with col_logo:
    st.markdown("""
    <div class="gd-topbar">
        <div class="gd-logo">🌿</div>
        <div style="flex:1">
            <div style="font-size:17px;font-weight:700;color:#1A1A2E;letter-spacing:-0.3px;">GreenData</div>
            <div style="font-size:12px;color:#7B8794;">SQL Аналитик &nbsp;·&nbsp; Qwen3-235B + RAG</div>
        </div>
    </div>
    """, unsafe_allow_html=True)
with col_reset:
    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)
    if st.button("🔄", help="Сбросить кэш (после смены API-ключа)"):
        st.cache_resource.clear()
        load_dotenv(override=True)
        st.success("Кэш сброшен — новый ключ подхвачен")
        st.rerun()

# ── Shared helpers ────────────────────────────────────────────────────────────

@st.cache_resource(show_spinner="Загружаем генератор...")
def get_generator():
    from generator.generator import GroqSQLGenerator
    return GroqSQLGenerator()


@st.cache_resource(show_spinner="Загружаем аудитор...")
def get_auditor():
    from auditor.auditor import GroqSecurityAuditor
    return GroqSecurityAuditor()


def risk_badge_html(score: float) -> str:
    if score < 3.0:
        return f'<span class="badge-low">🟢 Низкий риск &nbsp;{score:.1f}/10</span>'
    if score < 6.0:
        return f'<span class="badge-medium">🟡 Средний риск &nbsp;{score:.1f}/10</span>'
    return f'<span class="badge-high">🔴 Высокий риск &nbsp;{score:.1f}/10</span>'


def render_result(result: dict) -> None:
    sql = result.get("sql", "")
    gen_time = result.get("gen_time", 0.0)
    risk_score = result.get("risk_score", 0.0)
    approved = result.get("approved", True)
    summary = result.get("summary", "")
    vulns = result.get("vulnerabilities", [])
    tokens_total = result.get("tokens_total", 0)

    st.code(sql, language="sql")

    approved_str = "✅ Одобрен" if approved else "❌ Отклонён"
    tokens_str = f"{tokens_total:,}".replace(",", " ") if tokens_total else "—"
    st.markdown(f"""
    <div class="metric-row">
        <div class="metric-box">
            <strong>⏱ {gen_time:.1f} с</strong>
            Время генерации
        </div>
        <div class="metric-box">
            <strong>🔢 {tokens_str}</strong>
            Токенов использовано
        </div>
        <div class="metric-box" style="padding:7px 14px;">
            {risk_badge_html(risk_score)}
        </div>
        <div class="metric-box">
            <strong>{approved_str}</strong>
            Аудит безопасности
        </div>
    </div>
    """, unsafe_allow_html=True)

    if summary:
        st.caption(f"💬 {summary}")

    if vulns:
        rows = "".join(
            f'<div style="font-size:13px;color:#DC2626;padding:3px 0;">'
            f'<b>[{v["class"]}]</b> риск {v["score"]}/10 — {v["desc"]}</div>'
            for v in vulns
        )
        st.markdown(
            f'<div style="background:#FEF2F2;border:1px solid #FECACA;border-radius:10px;'
            f'padding:10px 14px;margin-top:6px;"><b style="font-size:13px;color:#DC2626;">'
            f'⚠️ Уязвимости ({len(vulns)})</b>{rows}</div>',
            unsafe_allow_html=True,
        )

# ── Tabs ──────────────────────────────────────────────────────────────────────

tab_chat, tab_dash, tab_hist = st.tabs(["💬  SQL Чат", "📊  Дашборд", "📜  История запросов"])

# ═══════════════════════════════════════════════════════════
# TAB 1 — CHAT
# ═══════════════════════════════════════════════════════════

with tab_chat:
    if "history" not in st.session_state:
        st.session_state.history = _load_history()
    if "last_result" not in st.session_state:
        st.session_state.last_result = None
    if "tokens_session" not in st.session_state:
        st.session_state.tokens_session = 0

    # st.chat_input всегда фиксирован внизу экрана независимо от места вызова.
    # Вызываем первым — чтобы знать, есть ли новый запрос, до рендера старого результата.
    user_query = st.chat_input("Опишите запрос на русском языке...")

    def _tok_counter() -> None:
        try:
            used_today = get_tokens_used_today()
            remaining = max(0, CEREBRAS_DAILY_LIMIT - used_today)
            limit_str = f"{CEREBRAS_DAILY_LIMIT:,}".replace(",", " ")
            rem_str = f"{remaining:,}".replace(",", " ")
            color = "#059669" if remaining > CEREBRAS_DAILY_LIMIT * 0.2 else "#DC2626"
            st.markdown(
                f'<div style="font-size:11px;color:#B0B8C1;text-align:right;margin-top:4px;">'
                f'<span style="color:{color};font-weight:600;">🔋 Остаток токенов: {rem_str}</span>'
                f'<span style="color:#B0B8C1;"> / {limit_str}</span></div>',
                unsafe_allow_html=True,
            )
        except Exception:
            pass


    if user_query:
        # ── Новый запрос: предыдущий результат не показываем ──
        with st.chat_message("user"):
            st.write(user_query)

        with st.chat_message("assistant", avatar="🌿"):
            try:
                with st.spinner("Генерирую SQL-запрос..."):
                    gen = get_generator()
                    t0 = time.time()
                    sql = gen.generate(task_description=user_query)
                    gen_time = time.time() - t0

                with st.spinner("Проверяю безопасность..."):
                    auditor = get_auditor()
                    audit = auditor.audit(sql)

                result = {
                    "query": user_query,
                    "sql": sql,
                    "gen_time": round(gen_time, 2),
                    "tokens_total": (
                        getattr(gen, "last_usage", {}).get("total_tokens", 0)
                        + getattr(auditor, "last_usage", {}).get("total_tokens", 0)
                    ),
                    "remaining_tokens": (
                        getattr(auditor, "last_usage", {}).get("remaining_tokens")
                        or getattr(gen, "last_usage", {}).get("remaining_tokens")
                    ),
                    "timestamp": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
                    "risk_score": audit.overall_risk_score,
                    "approved": audit.approved,
                    "summary": audit.summary,
                    "vulnerabilities": [
                        {
                            "class": v.vuln_class,
                            "score": v.risk_score,
                            "desc": v.description,
                        }
                        for v in audit.vulnerabilities
                    ],
                }
                render_result(result)
                st.session_state.history.append(result)
                st.session_state.last_result = result
                st.session_state.tokens_session += result.get("tokens_total", 0)
                _save_record(result)

            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower():
                    st.markdown("""
                    <div style="background:#FFFBEB;border:1px solid #FDE68A;border-radius:12px;padding:16px 20px;">
                        <div style="font-size:15px;font-weight:600;color:#D97706;margin-bottom:6px;">
                            ⏳ Дневной лимит токенов Cerebras исчерпан
                        </div>
                        <div style="font-size:13px;color:#92400E;line-height:1.6;">
                            Лимит восстановится завтра в 00:00 UTC (03:00 МСК).<br>
                            Можно поменять <code>CEREBRAS_API_KEY</code> в файле <code>.env</code> на другой аккаунт.
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
                else:
                    st.error(f"Ошибка: {err_str[:200]}")

        # Счётчик токенов показываем после генерации, когда tokens_session уже обновлён
        _tok_counter()

    elif st.session_state.last_result:
        # ── Режим ожидания: показываем только последний результат ──
        item = st.session_state.last_result
        with st.chat_message("user"):
            st.write(item["query"])
        with st.chat_message("assistant", avatar="🌿"):
            render_result(item)
        _tok_counter()

    else:
        # ── Приветственный экран ──
        st.markdown("""
        <div style="text-align:center;padding:48px 20px 32px;">
            <div style="font-size:52px;margin-bottom:14px;">🌿</div>
            <div style="font-size:21px;font-weight:700;color:#1A1A2E;margin-bottom:8px;">
                Добро пожаловать в SQL Аналитик
            </div>
            <div style="font-size:15px;color:#7B8794;max-width:460px;margin:0 auto;line-height:1.65;">
                Опишите задачу на русском языке — система сгенерирует SQL-запрос,
                проверит его безопасность и покажет метрики выполнения.
            </div>
        </div>
        """, unsafe_allow_html=True)

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown('<div class="hint-card">💼 Показать топ-10 активных сотрудников по фамилии</div>', unsafe_allow_html=True)
        with col2:
            st.markdown('<div class="hint-card">📋 Кредитные договоры за последние 30 дней</div>', unsafe_allow_html=True)
        with col3:
            st.markdown('<div class="hint-card">📊 Статистика заявок по статусам за квартал</div>', unsafe_allow_html=True)


# ═══════════════════════════════════════════════════════════
# TAB 2 — DASHBOARD
# ═══════════════════════════════════════════════════════════

with tab_dash:
    if not RESULTS_PATH.exists():
        st.markdown("""
        <div style="text-align:center;padding:60px 20px;color:#7B8794;">
            <div style="font-size:48px;margin-bottom:16px;">📊</div>
            <div style="font-size:18px;font-weight:600;color:#1A1A2E;margin-bottom:8px;">
                Нет данных для дашборда
            </div>
            <div style="font-size:14px;">
                Запусти валидацию:<br>
                <code style="background:#F5F7FA;padding:4px 10px;border-radius:6px;font-size:13px;">
                    .venv/bin/python validation/evaluate.py --limit 30
                </code>
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        results = json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
        valid = [r for r in results if r.get("error") is None]

        if not valid:
            st.warning("Нет валидных результатов в файле.")
        else:
            ea_vals = [r["execution_accuracy"] for r in valid]
            time_vals = [r["time_seconds"] for r in valid]
            overall_ea = mean(ea_vals)
            avg_time = mean(time_vals)
            n_passed = int(sum(ea_vals))
            n_errors = len([r for r in results if r.get("error")])
            gen_errors = len([r for r in valid if r.get("gen_error")])

            ea_color = "#3DC47A" if overall_ea >= 0.70 else "#D97706" if overall_ea >= 0.50 else "#DC2626"
            time_color = "#3DC47A" if avg_time <= 30 else "#D97706"

            # ── KPI Row ──────────────────────────────────────────────────
            c1, c2, c3, c4 = st.columns(4)

            with c1:
                st.markdown(f"""
                <div class="dash-card">
                    <div class="dash-value" style="color:{ea_color};">{n_passed}/{len(valid)}</div>
                    <div class="dash-label">Execution Accuracy — {overall_ea:.1%}</div>
                    <div class="dash-hint">{"✅ цель ≥ 70% достигнута" if overall_ea >= 0.7 else "❌ цель ≥ 70%"}</div>
                </div>
                """, unsafe_allow_html=True)

            with c2:
                st.markdown(f"""
                <div class="dash-card">
                    <div class="dash-value">{n_passed}</div>
                    <div class="dash-label">Совпавших запросов</div>
                    <div class="dash-hint">из {len(valid)} выполненных</div>
                </div>
                """, unsafe_allow_html=True)

            with c3:
                st.markdown(f"""
                <div class="dash-card">
                    <div class="dash-value" style="color:{time_color};">{avg_time:.1f}с</div>
                    <div class="dash-label">Среднее время</div>
                    <div class="dash-hint">{"✅ цель ≤ 30с" if avg_time <= 30 else "⚠️ превышает 30с"}</div>
                </div>
                """, unsafe_allow_html=True)

            with c4:
                st.markdown(f"""
                <div class="dash-card">
                    <div class="dash-value" style="color:#DC2626;">{n_errors}</div>
                    <div class="dash-label">Ошибок генератора</div>
                    <div class="dash-hint">из {len(results)} запросов</div>
                </div>
                """, unsafe_allow_html=True)

            st.markdown("<div style='height:20px'></div>", unsafe_allow_html=True)

            # ── By Complexity ─────────────────────────────────────────────
            st.markdown("#### По сложности запросов")
            cc1, cc2, cc3 = st.columns(3)

            for col, ctype, emoji in [
                (cc1, "simple",  "🟢 Simple"),
                (cc2, "medium",  "🟡 Medium"),
                (cc3, "complex", "🔴 Complex"),
            ]:
                subset = [r for r in valid if r.get("complexity") == ctype]
                if not subset:
                    continue
                cea = mean(r["execution_accuracy"] for r in subset)
                ctime = mean(r["time_seconds"] for r in subset)
                cea_color = "#3DC47A" if cea >= 0.7 else "#D97706" if cea >= 0.5 else "#DC2626"
                with col:
                    st.markdown(f"""
                    <div class="dash-card">
                        <div style="font-size:14px;font-weight:600;color:#1A1A2E;margin-bottom:10px;">{emoji}</div>
                        <div class="dash-value" style="color:{cea_color};font-size:30px;">{cea:.1%}</div>
                        <div class="dash-label">EA · {len(subset)} запросов</div>
                        <div class="dash-hint">avg {ctime:.1f}с</div>
                    </div>
                    """, unsafe_allow_html=True)

            st.markdown("<div style='height:24px'></div>", unsafe_allow_html=True)

            # ── Failed / Passed lists ─────────────────────────────────────
            col_l, col_r = st.columns(2)

            with col_l:
                failed = [r for r in valid if r["execution_accuracy"] == 0.0]
                st.markdown(f"#### ❌ Провальные запросы ({len(failed)})")
                for r in failed[:8]:
                    if r.get("gen_error"):
                        note = f"🚨 {r['gen_error'][:55]}"
                    elif r.get("gen_rows") == r.get("ref_rows"):
                        note = f"⚡ {r.get('gen_rows',0)} строк — разные данные"
                    else:
                        note = f"📊 gen={r.get('gen_rows',0)}, ref={r.get('ref_rows',0)}"
                    badge_map = {"simple": "badge-low", "medium": "badge-medium", "complex": "badge-high"}
                    bc = badge_map.get(r.get("complexity",""), "badge-low")
                    st.markdown(f"""
                    <div class="result-item">
                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
                            <span class="{bc}" style="padding:2px 8px;font-size:11px;">{r.get('complexity','')}</span>
                            <span style="font-size:13px;font-weight:600;color:#1A1A2E;">{r['task_id']}</span>
                        </div>
                        <div style="font-size:13px;color:#7B8794;margin-bottom:3px;">{r['task'][:68]}…</div>
                        <div style="font-size:12px;color:#DC2626;">{note}</div>
                    </div>
                    """, unsafe_allow_html=True)

            with col_r:
                passed = [r for r in valid if r["execution_accuracy"] == 1.0]
                st.markdown(f"#### ✅ Совпавшие запросы ({len(passed)})")
                for r in passed[:8]:
                    badge_map = {"simple": "badge-low", "medium": "badge-medium", "complex": "badge-high"}
                    bc = badge_map.get(r.get("complexity",""), "badge-low")
                    rows = r.get("gen_rows", 0)
                    st.markdown(f"""
                    <div class="result-item">
                        <div style="display:flex;align-items:center;gap:8px;margin-bottom:5px;">
                            <span class="{bc}" style="padding:2px 8px;font-size:11px;">{r.get('complexity','')}</span>
                            <span style="font-size:13px;font-weight:600;color:#1A1A2E;">{r['task_id']}</span>
                        </div>
                        <div style="font-size:13px;color:#7B8794;margin-bottom:3px;">{r['task'][:68]}…</div>
                        <div style="font-size:12px;color:#059669;">✅ {rows} строк совпало</div>
                    </div>
                    """, unsafe_allow_html=True)

            st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)
            if st.button("\U0001f504 Обновить дашборд"):
                st.rerun()

# ═══════════════════════════════════════════════════════════
# TAB 3 -- HISTORY
# ═══════════════════════════════════════════════════════════

with tab_hist:
    history = st.session_state.get("history", [])

    if not history:
        st.markdown("""
        <div style="text-align:center;padding:60px 20px;color:#7B8794;">
            <div style="font-size:48px;margin-bottom:16px;">📜</div>
            <div style="font-size:18px;font-weight:600;color:#1A1A2E;margin-bottom:8px;">
                История пуста
            </div>
            <div style="font-size:14px;line-height:1.6;">
                Перейдите на вкладку <b>SQL Чат</b> и задайте первый вопрос —<br>
                все запросы появятся здесь и сохранятся после перезагрузки.
            </div>
        </div>
        """, unsafe_allow_html=True)
    else:
        col_info, col_btn = st.columns([6, 1])
        with col_info:
            st.markdown(
                f"<div style='font-size:13px;color:#7B8794;padding-top:6px;'>"
                f"Всего запросов: <b>{len(history)}</b> · история сохраняется между сессиями</div>",
                unsafe_allow_html=True,
            )
        with col_btn:
            if st.button("🗑 Очистить", key="clear_hist"):
                st.session_state.history = []
                db_clear_history()
                st.rerun()

        st.markdown("<div style='height:12px'></div>", unsafe_allow_html=True)

        for idx, item in enumerate(reversed(history), start=1):
            ts = item.get("timestamp", "—")
            query = item.get("query", "")
            sql = item.get("sql", "")
            gen_time = item.get("gen_time", 0.0)
            tokens = item.get("tokens_total", 0)
            risk = item.get("risk_score", 0.0)
            approved = item.get("approved", True)

            risk_color = "#059669" if risk < 3.0 else "#D97706" if risk < 6.0 else "#DC2626"
            risk_label = "Низкий" if risk < 3.0 else "Средний" if risk < 6.0 else "Высокий"
            approved_icon = "✅" if approved else "❌"
            tokens_str = f"{tokens:,}".replace(",", " ") if tokens else "—"
            n = len(history) - idx + 1

            st.markdown(f"""
            <div class="result-item" style="margin-bottom:14px;">
                <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px;flex-wrap:wrap;">
                    <span style="font-size:12px;color:#7B8794;background:#F5F7FA;
                                 border:1px solid #E8ECF0;border-radius:6px;padding:2px 8px;">
                        #{n}
                    </span>
                    <span style="font-size:12px;color:#7B8794;">🕐 {ts}</span>
                    <span style="font-size:13px;font-weight:600;color:#1A1A2E;flex:1;min-width:0;
                                 overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">
                        {query}
                    </span>
                </div>
                <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px;">
                    <span style="font-size:12px;background:#F5F7FA;border:1px solid #E8ECF0;
                                 border-radius:8px;padding:4px 10px;color:#1A1A2E;">
                        ⏱ {gen_time:.1f} с
                    </span>
                    <span style="font-size:12px;background:#F5F7FA;border:1px solid #E8ECF0;
                                 border-radius:8px;padding:4px 10px;color:#1A1A2E;">
                        🔢 {tokens_str} токенов
                    </span>
                    <span style="font-size:12px;border-radius:8px;padding:4px 10px;font-weight:600;
                                 background:{risk_color}18;color:{risk_color};
                                 border:1px solid {risk_color}44;">
                        {risk_label} риск {risk:.1f}/10
                    </span>
                    <span style="font-size:12px;background:#F5F7FA;border:1px solid #E8ECF0;
                                 border-radius:8px;padding:4px 10px;color:#1A1A2E;">
                        {approved_icon} Аудит
                    </span>
                </div>
            </div>
            """, unsafe_allow_html=True)
            sql_escaped = _html.escape(sql)
            st.markdown(f"""
            <details style="margin-top:2px;">
              <summary style="cursor:pointer;font-size:12px;color:#7B8794;
                              list-style:none;outline:none;user-select:none;
                              padding:4px 0;display:flex;align-items:center;gap:5px;">
                <span style="font-size:10px;">▶</span> Показать SQL
              </summary>
              <pre style="background:#1E1E2E;color:#E2E8F0;padding:14px 16px;
                          border-radius:10px;font-size:12px;overflow-x:auto;
                          margin-top:8px;font-family:'Courier New',monospace;
                          line-height:1.6;">{sql_escaped}</pre>
            </details>
            """, unsafe_allow_html=True)
