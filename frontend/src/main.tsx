import { FormEvent, useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

type Role = "blue" | "red" | "spectator";
type Hero = { hero_id: string; name: string; title: string; icon_url: string; roles: string[]; win_rate?: number | null; pick_rate?: number | null; ban_rate?: number | null };
type Action = { phase_index: number; action_kind: "ban" | "pick"; team: "blue" | "red"; hero_id: string | null };
type State = {
  access_role?: Role;
  series: { code: string; best_of: number; global_draft: boolean; status: string };
  game: { number: number; status: string; blue_ready: boolean; red_ready: boolean; deadline_at: string | null; blue_preselect: string | null; red_preselect: string | null };
  current: { kind: "ban" | "pick"; team: "blue" | "red"; phase_index: number } | null;
  actions: Action[];
  heroes: Hero[];
  used_hero_ids: string[];
  global_used_hero_ids: string[];
};

async function request<T>(url: string, method = "GET", body?: unknown): Promise<T> {
  const response = await fetch(url, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined, credentials: "same-origin" });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "请求失败，请稍后重试。");
  return value as T;
}

function heroName(state: State | null, id: string | null): string {
  if (!id) return "空过";
  return state?.heroes.find((hero) => hero.hero_id === id)?.name ?? id;
}

function Room({ code, token }: { code: string; token: string }) {
  const [state, setState] = useState<State | null>(null);
  const [role, setRole] = useState<Role>("spectator");
  const [query, setQuery] = useState("");
  const [lane, setLane] = useState("ALL");
  const [selected, setSelected] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [seconds, setSeconds] = useState(0);

  const load = async () => {
    try {
      const snapshot = await request<State>(`/api/room/${code}/${token}/state`);
      setState(snapshot);
      if (snapshot.access_role) setRole(snapshot.access_role);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取赛事状态。"); }
  };
  useEffect(() => { load(); const timer = window.setInterval(load, 2500); return () => window.clearInterval(timer); }, [code, token]);
  useEffect(() => {
    const socket = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/room/${code}/${token}`);
    socket.onmessage = (event) => setState(JSON.parse(event.data));
    return () => socket.close();
  }, [code, token]);
  useEffect(() => {
    if (!state?.game.deadline_at) return setSeconds(0);
    const tick = () => setSeconds(Math.max(0, Math.ceil((new Date(state.game.deadline_at!).getTime() - Date.now()) / 1000)));
    tick(); const timer = window.setInterval(tick, 250); return () => window.clearInterval(timer);
  }, [state?.game.deadline_at]);

  const activeTeam = state?.current?.team;
  const ownTurn = role === activeTeam;
  const unavailable = new Set([...(state?.used_hero_ids ?? []), ...(state?.global_used_hero_ids ?? [])]);
  const heroes = useMemo(() => (state?.heroes ?? []).filter((hero) => {
    const haystack = `${hero.name} ${hero.title}`.toLocaleLowerCase();
    return (lane === "ALL" || hero.roles.includes(lane)) && haystack.includes(query.toLocaleLowerCase());
  }), [state, lane, query]);
  const action = async (path: string, body?: unknown) => { try { setError(""); const next = await request<State>(`/api/room/${code}/${token}/${path}`, "POST", body); setState(next); } catch (reason) { setError(reason instanceof Error ? reason.message : "操作失败。"); } };
  const select = async (hero: Hero) => {
    if (!ownTurn || unavailable.has(hero.hero_id)) return;
    setSelected(hero.hero_id); await action("preselect", { hero_id: hero.hero_id });
  };

  const blueActions = state?.actions.filter((item) => item.team === "blue") ?? [];
  const redActions = state?.actions.filter((item) => item.team === "red") ?? [];
  return <main className="draft-shell">
    <header className="draft-header"><div><p className="eyebrow">GLOBAL BANPICK · {state?.series.code ?? code}</p><h1>第 {state?.game.number ?? "-"} 局</h1></div><div className="status"><span>{state?.series.global_draft ? "全局 BP" : "常规 BP"}</span><strong>{state?.current ? `${state.current.team === "blue" ? "蓝色方" : "红色方"}${state.current.kind === "ban" ? "禁用" : "选择"}` : state?.game.status === "waiting_ready" ? "等待准备" : state?.series.status}</strong><b>{seconds ? `${seconds}s` : ""}</b></div></header>
    {error && <p className="error">{error}</p>}
    <section className="teams"><TeamPanel label="蓝色方" team="blue" actions={blueActions} state={state} ready={!!state?.game.blue_ready} /><section className="draft-center"><p>{role === "spectator" ? "观战模式" : role === "blue" ? "蓝色方队长" : "红色方队长"}</p>{state?.game.status === "waiting_ready" && role !== "spectator" && <button className="primary" onClick={() => action("ready")}>确认准备</button>}{state?.current && <p className="turn">{ownTurn ? "轮到你方" : "等待对方"}</p>}{ownTurn && state?.current && <button className="confirm" disabled={state.current.kind === "pick" && !selected} onClick={() => action("act", selected ? { hero_id: selected } : undefined)}>{state.current.kind === "ban" ? "确认禁用 / 空过" : "确认选择"}</button>}</section><TeamPanel label="红色方" team="red" actions={redActions} state={state} ready={!!state?.game.red_ready} /></section>
    <section className="roster"><div className="filters"><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索英雄或称号" />{[["ALL", "全部"], ["TOP", "上路"], ["JUNGLE", "打野"], ["MIDDLE", "中路"], ["BOTTOM", "下路"], ["UTILITY", "辅助"]].map(([value, label]) => <button key={value} className={lane === value ? "active" : ""} onClick={() => setLane(value)}>{label}</button>)}</div><div className="hero-grid">{heroes.map((hero) => <button key={hero.hero_id} className={`hero ${unavailable.has(hero.hero_id) ? "disabled" : ""} ${selected === hero.hero_id ? "selected" : ""}`} disabled={!ownTurn || unavailable.has(hero.hero_id)} onClick={() => select(hero)} title={unavailable.has(hero.hero_id) ? (state?.global_used_hero_ids.includes(hero.hero_id) ? "已在前局被选择" : "本局不可用") : hero.title}><img src={hero.icon_url} alt="" /><span>{hero.name}</span></button>)}</div></section>
  </main>;
}

function TeamPanel({ label, team, actions, state, ready }: { label: string; team: "blue" | "red"; actions: Action[]; state: State | null; ready: boolean }) {
  return <section className={`team ${team}`}><h2>{label}</h2><small>{ready ? "已准备" : "未准备"}</small><ol>{actions.map((item) => <li key={item.phase_index} className={item.action_kind}><span>{item.action_kind === "ban" ? "BAN" : "PICK"}</span>{heroName(state, item.hero_id)}</li>)}</ol></section>;
}

function Admin() {
  const [loggedIn, setLoggedIn] = useState(false); const [password, setPassword] = useState(""); const [series, setSeries] = useState<Array<{ code: string; best_of: number; global_draft: boolean; status: string }>>([]); const [bestOf, setBestOf] = useState(1); const [globalDraft, setGlobalDraft] = useState(true); const [refresh, setRefresh] = useState(0); const [limit, setLimit] = useState(1); const [message, setMessage] = useState(""); const [links, setLinks] = useState<Record<string, string> | null>(null);
  const load = async () => { try { await request("/api/admin/me"); setLoggedIn(true); const [items, config] = await Promise.all([request<typeof series>("/api/admin/series"), request<{ refresh_interval_seconds: number; max_active_matches: number }>("/api/admin/settings")]); setSeries(items); setRefresh(config.refresh_interval_seconds); setLimit(config.max_active_matches); } catch { setLoggedIn(false); } };
  useEffect(() => { load(); }, []);
  const run = async (operation: () => Promise<unknown>) => { try { setMessage(""); await operation(); await load(); setMessage("操作完成。"); } catch (reason) { setMessage(reason instanceof Error ? reason.message : "操作失败。"); } };
  if (!loggedIn) return <main className="admin-login"><div><p className="eyebrow">GLOBAL BANPICK</p><h1>赛事管理后台</h1><form onSubmit={(event: FormEvent) => { event.preventDefault(); run(async () => { await request("/api/admin/login", "POST", { password }); }); }}><input type="password" placeholder="管理员密码" value={password} onChange={(event) => setPassword(event.target.value)} /><button className="primary">登录</button></form>{message && <p className="error">{message}</p>}</div></main>;
  return <main className="admin"><header><div><p className="eyebrow">GLOBAL BANPICK</p><h1>赛事控制台</h1></div><button onClick={() => run(() => request("/api/admin/logout", "POST"))}>退出登录</button></header>{message && <p className="message">{message}</p>}<section className="admin-grid"><article><h2>创建赛事</h2><label>赛制<select value={bestOf} onChange={(event) => setBestOf(Number(event.target.value))}><option value={1}>BO1</option><option value={3}>BO3</option><option value={5}>BO5</option></select></label><label className="checkbox"><input type="checkbox" checked={globalDraft} onChange={(event) => setGlobalDraft(event.target.checked)} /> 全局 BP</label><button className="primary" onClick={() => run(async () => { const created = await request<Record<string, string>>("/api/admin/series", "POST", { best_of: bestOf, global_draft: globalDraft }); setLinks(created); })}>创建并生成链接</button>{links && <div className="links"><b>赛事 {links.code}</b>{["blue", "red", "spectator"].map((kind) => <label key={kind}>{kind === "blue" ? "蓝方" : kind === "red" ? "红方" : "观战"}<input readOnly value={links[kind] ?? ""} onFocus={(event) => event.currentTarget.select()} /></label>)}</div>}</article><article><h2>英雄数据</h2><p>OP.GG MCP 优先，公开英雄页备用。</p><button className="primary" onClick={() => run(() => request("/api/admin/sync", "POST"))}>立即同步英雄</button><label>自动同步秒数<input type="number" min="0" value={refresh} onChange={(event) => setRefresh(Number(event.target.value))} /></label><label>最大活跃赛事<input type="number" min="1" value={limit} onChange={(event) => setLimit(Number(event.target.value))} /></label><button onClick={() => run(() => request("/api/admin/settings", "PUT", { refresh_interval_seconds: refresh, max_active_matches: limit }))}>保存设置</button></article></section><section className="series-list"><h2>最近赛事</h2>{series.map((item) => <article key={item.code}><strong>{item.code}</strong><span>BO{item.best_of} · {item.global_draft ? "全局" : "常规"} · {item.status}</span><div><button onClick={() => run(() => request(`/api/admin/series/${item.code}/next`, "POST"))}>下一局</button><button className="danger" onClick={() => run(() => request(`/api/admin/series/${item.code}/end`, "POST"))}>结束</button></div></article>)}</section></main>;
}

const parts = location.pathname.split("/").filter(Boolean);
createRoot(document.getElementById("root")!).render(parts[0] === "room" && parts[1] && parts[2] ? <Room code={parts[1]} token={parts[2]} /> : <Admin />);
