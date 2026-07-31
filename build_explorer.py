# build_explorer.py — React/JSX template for the Distributional Shape Explorer.
# build_html.py regex-extracts the TEMPLATE r-string below and injects merged data
# in place of __DATA__. Do not add code outside the TEMPLATE string.

TEMPLATE = r'''import { useState, useMemo } from "react";

const DATA = __DATA__;

const THEMES = {
  dark: {
    "--bg":"#060c18", "--card":"#0d1830", "--subtle":"#0d1830", "--sel":"#16294a",
    "--border":"#1b2b47", "--track":"#0b1220", "--inactive":"#0d1830", "--ring":"#0a1220",
    "--chipbg":"#16294a", "--chipbd":"#22375c", "--chipfg":"#93c5fd",
    "--text":"#f1f5f9", "--text2":"#dbe6f5", "--text3":"#c7d5ea",
    "--label":"#9db0cc", "--desc":"#6b82a3", "--faint":"#647b9c",
    "--green":"#34d399", "--amber":"#fbbf24", "--red":"#f87171",
    "--orange":"#fb923c", "--blue":"#60a5fa", "--purple":"#a78bfa",
    "--accent":"#2563eb", "--accent2":"#3b82f6", "--onaccent":"#ffffff",
    "--greenBg":"rgba(52,211,153,.14)", "--amberBg":"rgba(251,191,36,.14)", "--redBg":"rgba(248,113,113,.14)",
    "--fillA":"#5eead4", "--fillB":"#60a5fa", "--fillC":"#fbbf24", "--fillD":"#fb7185",
    "--phChange":"#34d399", "--phDiag":"#fbbf24", "--phCoord":"#60a5fa", "--phAdmin":"#7c8ba1",
    "--barlabel":"#06121f",
    "--warnbg":"#3a2c0c", "--warnbd":"#6b5310", "--warntitle":"#fbbf24", "--warntext":"#c9b071",
  },
  light: {
    "--bg":"#f1f5f9", "--card":"#ffffff", "--subtle":"#f8fafc", "--sel":"#eff6ff",
    "--border":"#e2e8f0", "--track":"#e2e8f0", "--inactive":"#f1f5f9", "--ring":"#ffffff",
    "--chipbg":"#eff6ff", "--chipbd":"#bfdbfe", "--chipfg":"#1d4ed8",
    "--text":"#0f172a", "--text2":"#1e293b", "--text3":"#334155",
    "--label":"#475569", "--desc":"#64748b", "--faint":"#6b7a8f",
    "--green":"#047857", "--amber":"#b45309", "--red":"#dc2626",
    "--orange":"#c2410c", "--blue":"#2563eb", "--purple":"#7c3aed",
    "--accent":"#2563eb", "--accent2":"#3b82f6", "--onaccent":"#ffffff",
    "--greenBg":"#ecfdf5", "--amberBg":"#fffbeb", "--redBg":"#fef2f2",
    "--fillA":"#5eead4", "--fillB":"#93c5fd", "--fillC":"#fcd34d", "--fillD":"#fda4af",
    "--phChange":"#6ee7b7", "--phDiag":"#fcd34d", "--phCoord":"#93c5fd", "--phAdmin":"#cbd5e1",
    "--barlabel":"#0f172a",
    "--warnbg":"#fffbeb", "--warnbd":"#fcd34d", "--warntitle":"#b45309", "--warntext":"#92400e",
  },
};

const V = n => `var(--${n})`;
const CX_COLOR = { low:V("green"), medium:V("amber"), high:V("red"), "":V("faint") };
const CX_BG    = { low:V("greenBg"), medium:V("amberBg"), high:V("redBg"), "":V("subtle") };
const BK = ["a","b","c","d"];
const BK_LABEL = { a:"<0.5h", b:"0.5-2h", c:"2-6h", d:">6h" };
const BK_COLOR = { a:V("fillA"), b:V("fillB"), c:V("fillC"), d:V("fillD") };

const PHASES = ["change","diagnose","coordinate","admin"];
const PH_COLOR = { change:V("phChange"), diagnose:V("phDiag"), coordinate:V("phCoord"), admin:V("phAdmin") };

const cvColor   = cv => cv < 1.3 ? V("green") : cv < 2 ? V("amber") : V("red");
const tailColor = t  => t  < 3   ? V("green") : t  < 6 ? V("amber") : V("red");
const procColor = s  => s >= 60  ? V("green") : s >= 45 ? V("amber") : V("red");
const jacColor  = j  => j >= 0.4 ? V("green") : j >= 0.2 ? V("amber") : V("red");
const entColor  = e  => e < 0.5  ? V("green") : e < 0.8 ? V("amber") : V("red");
// >=40% of tickets yielded no extractable action -> the pattern stats are
// dominated by the empty pattern and the score is not trustworthy.
const UNRELIABLE = 40;
const isShaky = pr => pr && pr.noact >= UNRELIABLE;

function StackBar({ b, height=26, showLabels=false }) {
  return (
    <div style={{ display:"flex", height, borderRadius:5, overflow:"hidden", width:"100%", background:V("track") }}>
      {BK.map(k => {
        const v = b[k] || 0;
        return (
          <div key={k} title={`${BK_LABEL[k]}: ${v}%`} style={{ width:`${v}%`, background:BK_COLOR[k], position:"relative", transition:"width .3s" }}>
            {showLabels && v > 9 && <span style={{ position:"absolute", inset:0, display:"flex", alignItems:"center", justifyContent:"center", fontSize:10, fontWeight:800, color:V("barlabel") }}>{v}%</span>}
          </div>
        );
      })}
    </div>
  );
}

function PhaseBar({ ph, height=24, showLabels=true }) {
  return (
    <div>
      <div style={{ display:"flex", height, borderRadius:5, overflow:"hidden", background:V("track") }}>
        {PHASES.map(p => {
          const v = ph[p] || 0;
          return (
            <div key={p} title={`${p}: ${v}%`} style={{ width:`${v}%`, background:PH_COLOR[p], position:"relative" }}>
              {showLabels && v > 12 && <span style={{ position:"absolute", inset:0, display:"flex", alignItems:"center", justifyContent:"center", fontSize:9.5, fontWeight:800, color:V("barlabel") }}>{v}%</span>}
            </div>
          );
        })}
      </div>
      <div style={{ display:"flex", gap:12, marginTop:6, flexWrap:"wrap" }}>
        {PHASES.map(p => (
          <span key={p} style={{ fontSize:10, color:V("label") }}>
            <span style={{ display:"inline-block", width:9, height:9, borderRadius:2, background:PH_COLOR[p], marginRight:4, verticalAlign:"-1px" }} />
            {p} <b style={{ color:V("text2") }}>{ph[p]||0}%</b>
          </span>
        ))}
      </div>
    </div>
  );
}

function Metric({ label, value, sub, color }) {
  const c = color || V("text");
  return (
    <div style={{ background:V("card"), border:`1px solid ${V("border")}`, borderRadius:9, padding:"9px 13px", minWidth:74, flex:"1 1 auto" }}>
      <div style={{ fontSize:9.5, color:V("label"), fontWeight:700, letterSpacing:".09em", textTransform:"uppercase" }}>{label}</div>
      <div style={{ fontSize:20, fontWeight:800, color:c, lineHeight:1.15, marginTop:3 }}>{value}</div>
      {sub && <div style={{ fontSize:9.5, color:V("desc"), marginTop:1 }}>{sub}</div>}
    </div>
  );
}

function Bar01({ v, max, color }) {
  return (
    <div style={{ flex:1, height:7, background:V("track"), borderRadius:4, overflow:"hidden" }}>
      <div style={{ width:`${Math.min(v/max*100,100)}%`, height:"100%", background:color, borderRadius:4 }} />
    </div>
  );
}

function CVMeter({ cv, max=4 }) {
  return (
    <div style={{ display:"flex", alignItems:"center", gap:10 }}>
      <Bar01 v={cv} max={max} color={cvColor(cv)} />
      <span style={{ fontSize:12, fontWeight:800, color:cvColor(cv), minWidth:32 }}>{cv.toFixed(2)}</span>
    </div>
  );
}

function PercentileStrip({ s }) {
  const pts = [
    { l:"p25", v:s.p25, c:V("blue") }, { l:"med", v:s.med, c:V("purple") },
    { l:"p75", v:s.p75, c:V("amber") }, { l:"p90", v:s.p90, c:V("orange") },
    { l:"p95", v:s.p95, c:V("red") },
  ];
  const mx = Math.max(s.p95 * 1.15, 0.1);
  return (
    <div style={{ background:V("card"), border:`1px solid ${V("border")}`, borderRadius:9, padding:13 }}>
      <div style={{ position:"relative", height:26 }}>
        <div style={{ position:"absolute", top:"50%", left:0, right:0, height:3, background:V("track"), borderRadius:2, transform:"translateY(-50%)" }} />
        {pts.map(p => (
          <div key={p.l} style={{ position:"absolute", left:`${Math.min(p.v/mx*100,100)}%`, top:"50%", transform:"translate(-50%,-50%)" }}>
            <div style={{ width:11, height:11, borderRadius:"50%", background:p.c, border:`2px solid ${V("ring")}` }} />
          </div>
        ))}
      </div>
      <div style={{ display:"flex", gap:10, flexWrap:"wrap", marginTop:6 }}>
        {pts.map(p => <span key={p.l} style={{ fontSize:10, color:V("label") }}><span style={{ display:"inline-block", width:7, height:7, borderRadius:"50%", background:p.c, marginRight:3 }} />{p.l} <b style={{ color:V("text2") }}>{p.v}h</b></span>)}
        <span style={{ fontSize:10, color:V("desc") }}>max {s.max}h</span>
      </div>
    </div>
  );
}

function ShakyBadge({ pr, compact }) {
  if (!isShaky(pr)) return null;
  if (compact) return <span title={`${pr.noact}% of sampled tickets had no extractable action`} style={{ fontSize:9, color:V("amber") }}>⚠</span>;
  return (
    <div style={{ background:V("warnbg"), border:`1px solid ${V("warnbd")}`, borderRadius:8, padding:"9px 12px", marginBottom:12 }}>
      <div style={{ fontSize:11.5, color:V("amber"), fontWeight:700, marginBottom:3 }}>⚠ Score not trustworthy for this workflow</div>
      <div style={{ fontSize:10.5, color:V("warntext"), lineHeight:1.5 }}>
        No concrete action could be extracted from <b>{pr.noact}%</b> of sampled tickets. The dominant
        &ldquo;pattern&rdquo; here is the <i>empty</i> one, which inflates pattern-concentration and pushes the
        score up. Read this as <b>unmeasured</b>, not as consistent. Usually means the notes are too terse,
        or the work happened outside the ticket.
      </div>
    </div>
  );
}

// Section heading with a one-line explanation underneath.
function Heading({ title, desc, top }) {
  return (
    <div style={{ margin: top ? "16px 0 8px" : "0 0 8px" }}>
      <div style={{ fontSize:10, color:V("label"), fontWeight:700, textTransform:"uppercase", letterSpacing:".08em" }}>{title}</div>
      {desc && <div style={{ fontSize:10, color:V("desc"), marginTop:3, lineHeight:1.45 }}>{desc}</div>}
    </div>
  );
}

// A labelled metric row inside the variance card: name, plain-English gloss, value.
function VarRow({ label, desc, children }) {
  return (
    <div>
      <div style={{ display:"flex", justifyContent:"space-between", alignItems:"baseline", gap:12, marginBottom:4 }}>
        <span style={{ fontSize:11, color:V("label"), fontWeight:600 }}>{label}</span>
        <span style={{ fontSize:9.5, color:V("desc"), textAlign:"right" }}>{desc}</span>
      </div>
      {children}
    </div>
  );
}

function StepChips({ steps }) {
  if (!steps || steps.length === 0)
    return <span style={{ fontSize:11, color:V("warntitle"), fontStyle:"italic" }}>(no action extracted)</span>;
  return (
    <span style={{ display:"inline-flex", flexWrap:"wrap", gap:4 }}>
      {steps.map((s, i) => {
        const [action, object] = s.split(":");
        return (
          <span key={i} style={{ fontSize:10, background:V("chipbg"), border:`1px solid ${V("chipbd")}`, borderRadius:4, padding:"1px 6px", whiteSpace:"nowrap" }}>
            <span style={{ color:V("chipfg"), fontWeight:600 }}>{action}</span>
            <span style={{ color:V("label") }}>:{object}</span>
          </span>
        );
      })}
    </span>
  );
}

function ProcessBlock({ pr }) {
  if (!pr) return <div style={{ fontSize:12, color:V("desc") }}>No extraction data for this workflow.</div>;
  const maxPat = pr.pats[0]?.pct || 1;
  return (
    <div>
      <ShakyBadge pr={pr} />
      <div style={{ display:"flex", gap:8, flexWrap:"wrap", marginBottom:14 }}>
        <Metric label="Process score" value={pr.score} color={procColor(pr.score)} sub="0-100 · how repeatable" />
        <Metric label="Patterns" value={pr.npat.toLocaleString()} sub={`distinct routes in ${pr.n.toLocaleString()}`} />
        <Metric label="Top-3 cover" value={`${pr.top3}%`} sub="tickets on 3 routes" />
        <Metric label="Mean Jaccard" value={pr.jac.toFixed(2)} color={jacColor(pr.jac)} sub="step overlap, 2 tickets" />
        <Metric label="Steps/ticket" value={pr.steps.toFixed(1)} sub="distinct actions" />
      </div>

      <div style={{ background:V("card"), border:`1px solid ${V("border")}`, borderRadius:9, padding:13, marginBottom:14, display:"flex", flexDirection:"column", gap:13 }}>
        <VarRow label="Pattern entropy" desc="how spread out the routes are · 0 = every ticket identical, 1 = every ticket unique">
          <div style={{ display:"flex", alignItems:"center", gap:10 }}>
            <Bar01 v={pr.ent} max={1} color={entColor(pr.ent)} />
            <span style={{ fontSize:12, fontWeight:800, color:entColor(pr.ent), minWidth:34 }}>{pr.ent.toFixed(2)}</span>
          </div>
        </VarRow>

        <VarRow label="Bimodality" desc="are there two distinct groups? · 0 = one population, 1 = a clean split">
          <div style={{ display:"flex", alignItems:"center", gap:10 }}>
            <Bar01 v={pr.bimod} max={1} color={pr.bimod >= 0.45 ? V("orange") : V("faint")} />
            <span style={{ fontSize:12, fontWeight:800, color: pr.bimod >= 0.45 ? V("orange") : V("label"), minWidth:34 }}>{pr.bimod.toFixed(2)}</span>
          </div>
          {pr.bimod >= 0.45 && (
            <div style={{ fontSize:10, color:V("orange"), marginTop:5 }}>
              Split candidate — one label is covering two different kinds of work.
            </div>
          )}
        </VarRow>

        <VarRow label="Action vocabulary" desc="how many different action:object steps appear at all">
          <span style={{ fontSize:13, fontWeight:800, color:V("text3") }}>{pr.npairs}</span>
          <span style={{ fontSize:10, color:V("desc"), marginLeft:7 }}>distinct steps · {pr.steps.toFixed(1)} used per ticket on average</span>
        </VarRow>

        <VarRow label="No action extracted" desc="notes too terse, or work done outside the ticket">
          <span style={{ fontSize:13, fontWeight:800, color: pr.noact >= UNRELIABLE ? V("amber") : pr.noact >= 20 ? V("text3") : V("label") }}>{pr.noact}%</span>
          <span style={{ fontSize:10, color:V("desc"), marginLeft:7 }}>of tickets</span>
        </VarRow>
      </div>

      <Heading title="Work-type mix"
        desc="What kind of work the steps are. Change-heavy is executable work; diagnose-heavy means investigation you can't time in advance; coordinate-heavy is mostly waiting on other people." />
      <PhaseBar ph={pr.ph} />

      <Heading top title="Most common resolution patterns"
        desc="A pattern is the full set of steps in one ticket. Two tickets share a pattern if the same work was done, whatever the wording or order." />
      <div style={{ display:"flex", flexDirection:"column", gap:7 }}>
        {pr.pats.map((p, i) => (
          <div key={i} style={{ display:"flex", alignItems:"flex-start", gap:9 }}>
            <div style={{ width:70, flexShrink:0, paddingTop:3 }}>
              <div style={{ height:5, background:V("track"), borderRadius:3, overflow:"hidden" }}>
                <div style={{ width:`${Math.min(p.pct/maxPat*100,100)}%`, height:"100%", background:V("accent"), borderRadius:3 }} />
              </div>
            </div>
            <span style={{ fontSize:10.5, color:V("label"), width:36, flexShrink:0, fontWeight:700 }}>{p.pct}%</span>
            <div style={{ flex:1, minWidth:0 }}><StepChips steps={p.p} /></div>
          </div>
        ))}
      </div>

      <Heading top title="Most common individual steps"
        desc="Single steps counted across all tickets, ignoring what they were combined with. % = share of tickets containing that step." />
      <div style={{ display:"flex", flexWrap:"wrap", gap:5 }}>
        {pr.pairs.map((p, i) => {
          const [a, o] = p.p.split(":");
          return (
            <span key={i} style={{ fontSize:10.5, background:V("subtle"), border:`1px solid ${V("border")}`, borderRadius:5, padding:"3px 8px" }}>
              <span style={{ color:V("chipfg"), fontWeight:600 }}>{a}</span>
              <span style={{ color:V("label") }}>:{o}</span>
              <span style={{ color:V("text3"), fontWeight:700, marginLeft:6 }}>{p.pct}%</span>
            </span>
          );
        })}
      </div>

      <div style={{ fontSize:9.5, color:V("desc"), marginTop:14, paddingTop:10, borderTop:`1px solid ${V("border")}`, lineHeight:1.5 }}>
        Steps were extracted from the Dutch ticket notes by an LLM against a fixed action:object
        vocabulary, then each ticket reduced to the unordered set of its steps. Order is discarded.
      </div>
    </div>
  );
}

function WorkflowDetail({ wf }) {
  const rec = DATA.workflows[wf];
  const g = rec.g, pr = rec.pr;
  const [comp, setComp] = useState("__all__");
  const [tab, setTab] = useState("effort");
  const companies = Object.keys(rec.by).sort((a,b)=> rec.by[b].n - rec.by[a].n);
  const s = comp === "__all__" ? g : rec.by[comp];
  const compRows = Object.entries(rec.by).filter(([,v])=>v.n>=5).sort((a,b)=> a[1].cv - b[1].cv);

  return (
    <div>
      <div style={{ marginBottom:2 }}>
        <span style={{ fontSize:9.5, background:CX_BG[rec.cx], color:CX_COLOR[rec.cx], borderRadius:4, padding:"2px 8px", fontWeight:800, letterSpacing:".05em" }}>{(rec.cx||"—").toUpperCase()}</span>
        <span style={{ fontSize:11, color:V("desc"), marginLeft:8 }}>{rec.cat}</span>
        {pr && <span style={{ fontSize:11, marginLeft:8, fontWeight:700, color:procColor(pr.score) }}>· process {pr.score}</span>}
        {isShaky(pr) && <span style={{ fontSize:11, marginLeft:6, color:V("amber") }}>⚠</span>}
      </div>
      <h2 style={{ color:V("text"), fontSize:17, fontWeight:800, margin:"7px 0 14px", lineHeight:1.25 }}>{wf}</h2>

      <div style={{ display:"flex", background:V("inactive"), borderRadius:7, padding:3, marginBottom:16, width:"fit-content" }}>
        {[["effort","Effort distribution"],["process","Process variance"]].map(([k,l])=>(
          <button key={k} onClick={()=>setTab(k)} style={{ padding:"5px 14px", borderRadius:5, border:"none", cursor:"pointer", fontSize:11.5, fontWeight:700, background: tab===k ? V("accent") : "transparent", color: tab===k ? V("onaccent") : V("label") }}>{l}</button>
        ))}
      </div>

      {tab === "process" ? <ProcessBlock pr={pr} /> : (
        <>
          <div style={{ fontSize:10, color:V("label"), fontWeight:700, marginBottom:6, textTransform:"uppercase", letterSpacing:".08em" }}>Slice by company</div>
          <div style={{ display:"flex", flexWrap:"wrap", gap:5, marginBottom:16 }}>
            <Chip active={comp==="__all__"} onClick={()=>setComp("__all__")} label={`All (${g.n.toLocaleString()})`} />
            {companies.map(c => <Chip key={c} active={comp===c} onClick={()=>setComp(c)} label={`${c} (${rec.by[c].n})`} />)}
          </div>

          <div style={{ display:"flex", gap:8, flexWrap:"wrap", marginBottom:16 }}>
            <Metric label="Tickets" value={s.n.toLocaleString()} />
            <Metric label="AHT" value={`${s.aht}m`} />
            <Metric label="FRR" value={`${s.frr}%`} sub="1-touch" />
            <Metric label="Touches" value={s.tch.toFixed(2)} />
          </div>

          <SectionLabel>Effort distribution — hours per ticket</SectionLabel>
          <StackBar b={s.b} height={38} showLabels />
          <Legend />

          <SectionLabel top>Numeric variance</SectionLabel>
          <div style={{ background:V("card"), border:`1px solid ${V("border")}`, borderRadius:9, padding:13, display:"flex", flexDirection:"column", gap:11 }}>
            <div>
              <div style={{ display:"flex", justifyContent:"space-between", marginBottom:4 }}>
                <span style={{ fontSize:11, color:V("label") }}>Hours CV</span>
                <span style={{ fontSize:9.5, color:V("desc") }}>tight &lt;1.3 · spread &gt;2</span>
              </div>
              <CVMeter cv={s.cv} />
            </div>
            <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center" }}>
              <span style={{ fontSize:11, color:V("label") }}>Tail ratio (p90 ÷ median)</span>
              <span style={{ fontSize:13, fontWeight:800, color:tailColor(s.tail) }}>×{s.tail.toFixed(1)}</span>
            </div>
          </div>

          <SectionLabel top>Hours percentiles</SectionLabel>
          <PercentileStrip s={s} />

          {comp==="__all__" && compRows.length > 1 && (
            <>
              <SectionLabel top>Per-company shape (sorted by consistency)</SectionLabel>
              <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
                {compRows.map(([c,v]) => (
                  <div key={c} style={{ display:"flex", alignItems:"center", gap:8, cursor:"pointer" }} onClick={()=>setComp(c)}>
                    <div style={{ width:66, fontSize:10.5, color:V("label"), textAlign:"right", flexShrink:0 }}>{c}</div>
                    <div style={{ flex:1 }}><StackBar b={v.b} height={16} /></div>
                    <div style={{ width:60, textAlign:"right", fontSize:10, color:cvColor(v.cv), fontWeight:700 }}>cv {v.cv.toFixed(2)}</div>
                    <div style={{ width:38, textAlign:"right", fontSize:10, color:V("label") }}>{v.n}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  );
}

function CompanyDetail({ company }) {
  const [sortBy, setSortBy] = useState("n");
  const summary = DATA.company_summary[company];
  const rows = DATA.wf_order
    .filter(wf => DATA.workflows[wf].by[company])
    .map(wf => ({ wf, cat:DATA.workflows[wf].cat, cx:DATA.workflows[wf].cx, ...DATA.workflows[wf].by[company] }))
    .sort((a,b) => sortBy==="n" ? b.n-a.n : sortBy==="aht" ? b.aht-a.aht : sortBy==="cv" ? b.cv-a.cv : sortBy==="tail" ? b.tail-a.tail : b.frr-a.frr);
  const totalTickets = rows.reduce((s,r)=>s+r.n,0);
  return (
    <div>
      <div style={{ fontSize:11, color:V("desc"), marginBottom:2 }}>Company profile</div>
      <h2 style={{ color:V("text"), fontSize:19, fontWeight:800, margin:"3px 0 15px" }}>{company}</h2>
      <div style={{ display:"flex", gap:8, flexWrap:"wrap", marginBottom:16 }}>
        <Metric label="Tickets" value={totalTickets.toLocaleString()} />
        <Metric label="Workflows" value={rows.length} />
        <Metric label="Overall AHT" value={`${summary.aht}m`} />
        <Metric label="Overall FRR" value={`${summary.frr}%`} />
      </div>
      <SectionLabel>Company-wide effort mix</SectionLabel>
      <StackBar b={summary.b} height={34} showLabels />
      <Legend />
      <SectionLabel top>Workflows within {company}</SectionLabel>
      <div style={{ display:"flex", gap:5, marginBottom:8 }}>
        {[["n","Volume"],["aht","AHT"],["cv","CV"],["tail","Tail"],["frr","FRR"]].map(([k,l])=>(
          <button key={k} onClick={()=>setSortBy(k)} style={{ fontSize:10.5, padding:"3px 9px", borderRadius:5, border:"none", cursor:"pointer", fontWeight:700, background: sortBy===k ? V("accent") : V("inactive"), color: sortBy===k ? V("onaccent") : V("label") }}>{l}</button>
        ))}
      </div>
      <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
        {rows.map(r => (
          <div key={r.wf} style={{ display:"flex", alignItems:"center", gap:8 }}>
            <div style={{ width:6, height:26, borderRadius:2, background:CX_COLOR[r.cx], flexShrink:0 }} />
            <div style={{ width:150, flexShrink:0 }}>
              <div style={{ fontSize:10.5, color:V("text3"), lineHeight:1.15, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }} title={r.wf}>{r.wf}</div>
              <div style={{ fontSize:9, color:V("desc") }}>{r.n} tix · {r.aht}m</div>
            </div>
            <div style={{ flex:1 }}><StackBar b={r.b} height={20} /></div>
            <div style={{ width:52, textAlign:"right", fontSize:10, color:cvColor(r.cv), fontWeight:700 }}>cv {r.cv.toFixed(2)}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── PROCESS MODE: process consistency vs effort consistency ──
function ProcessView({ onOpenWorkflow }) {
  const [hover, setHover] = useState(null);
  const [hideShaky, setHideShaky] = useState(true);
  const [sortBy, setSortBy] = useState("score");

  const all = DATA.wf_order
    .filter(wf => DATA.workflows[wf].pr)
    .map(wf => ({ wf, cat:DATA.workflows[wf].cat, cx:DATA.workflows[wf].cx, g:DATA.workflows[wf].g, pr:DATA.workflows[wf].pr }));
  const items = hideShaky ? all.filter(x => !isShaky(x.pr)) : all;
  const nShaky = all.length - all.filter(x => !isShaky(x.pr)).length;

  const ranked = [...items].sort((a,b) =>
    sortBy==="score" ? b.pr.score-a.pr.score :
    sortBy==="jac"   ? b.pr.jac-a.pr.jac :
    sortBy==="ent"   ? a.pr.ent-b.pr.ent :
    sortBy==="bimod" ? b.pr.bimod-a.pr.bimod :
                       b.g.n-a.g.n);

  // x = process score, y = hours CV (axis inverted so tighter effort sits higher)
  const W=560, H=400, PAD=48;
  const xMin=25, xMax=92, yMax=5;
  const sx = v => PAD + ((Math.min(Math.max(v,xMin),xMax)-xMin)/(xMax-xMin))*(W-2*PAD);
  const sy = v => PAD + (Math.min(v,yMax)/yMax)*(H-2*PAD);   // low CV -> top
  const rSize = n => 4 + Math.sqrt(n)/9;
  const XSPLIT=50, YSPLIT=2.0;

  return (
    <div style={{ display:"flex", gap:20, height:"100%" }}>
      <div style={{ flex:"0 0 auto" }}>
        <SectionLabel>Process consistency × effort consistency</SectionLabel>
        <div style={{ position:"relative", background:V("card"), border:`1px solid ${V("border")}`, borderRadius:12, padding:8 }}>
          <svg width={W} height={H} style={{ display:"block" }}>
            <line x1={sx(XSPLIT)} y1={PAD} x2={sx(XSPLIT)} y2={H-PAD} stroke={V("border")} strokeDasharray="4 4" />
            <line x1={PAD} y1={sy(YSPLIT)} x2={W-PAD} y2={sy(YSPLIT)} stroke={V("border")} strokeDasharray="4 4" />
            <line x1={PAD} y1={H-PAD} x2={W-PAD} y2={H-PAD} stroke={V("faint")} />
            <line x1={PAD} y1={PAD} x2={PAD} y2={H-PAD} stroke={V("faint")} />
            {[30,40,50,60,70,80,90].map(t => <text key={t} x={sx(t)} y={H-PAD+16} fontSize="9" fill={V("label")} textAnchor="middle">{t}</text>)}
            {[0,1,2,3,4,5].map(t => <text key={t} x={PAD-8} y={sy(t)+3} fontSize="9" fill={V("label")} textAnchor="end">{t}</text>)}
            <text x={W/2} y={H-8} fontSize="10" fill={V("label")} textAnchor="middle">Process score (same steps →)</text>
            <text x={13} y={H/2} fontSize="10" fill={V("label")} textAnchor="middle" transform={`rotate(-90 13 ${H/2})`}>Hours CV (← tighter effort)</text>

            <text x={sx(72)} y={PAD+16} fontSize="10" fill={V("green")} opacity="0.7" textAnchor="middle" fontWeight="700">playbook</text>
            <text x={sx(36)} y={PAD+16} fontSize="10" fill={V("blue")} opacity="0.7" textAnchor="middle" fontWeight="700">standardise procedure</text>
            <text x={sx(72)} y={H-PAD-10} fontSize="10" fill={V("amber")} opacity="0.7" textAnchor="middle" fontWeight="700">add triage</text>
            <text x={sx(36)} y={H-PAD-10} fontSize="10" fill={V("red")} opacity="0.7" textAnchor="middle" fontWeight="700">split the workflow</text>

            {items.map(it => {
              const on = hover===it.wf;
              return (
                <circle key={it.wf} cx={sx(it.pr.score)} cy={sy(it.g.cv)} r={rSize(it.g.n)}
                  fill={procColor(it.pr.score)} fillOpacity={on?0.95:0.5}
                  stroke={V("ring")} strokeWidth={on?2:1} style={{ cursor:"pointer" }}
                  onMouseEnter={()=>setHover(it.wf)} onMouseLeave={()=>setHover(null)}
                  onClick={()=>onOpenWorkflow(it.wf)} />
              );
            })}
          </svg>
          {hover && (() => {
            const it = items.find(x=>x.wf===hover);
            return (
              <div style={{ position:"absolute", top:12, right:12, background:V("card"), border:`1px solid ${V("border")}`, borderRadius:8, padding:"8px 11px", maxWidth:220, pointerEvents:"none" }}>
                <div style={{ fontSize:11.5, fontWeight:700, color:V("text"), marginBottom:3, lineHeight:1.25 }}>{it.wf}</div>
                <div style={{ fontSize:10, color:V("label"), lineHeight:1.5 }}>
                  process <b style={{color:procColor(it.pr.score)}}>{it.pr.score}</b> · jaccard <b style={{color:jacColor(it.pr.jac)}}>{it.pr.jac.toFixed(2)}</b><br/>
                  {it.pr.npat} patterns in {it.pr.n} · CV <b style={{color:cvColor(it.g.cv)}}>{it.g.cv.toFixed(2)}</b> · {it.g.n.toLocaleString()} tix
                </div>
              </div>
            );
          })()}
        </div>
        <div style={{ fontSize:10, color:V("label"), marginTop:8, lineHeight:1.55, maxWidth:W }}>
          Bubble = workflow, size = ticket volume. <b style={{color:V("green")}}>Top-right</b>: same steps, same
          effort — write the runbook. <b style={{color:V("blue")}}>Top-left</b>: consistent time but many
          different routes — standardise the procedure. <b style={{color:V("amber")}}>Bottom-right</b>: consistent
          steps but effort blows up on some tickets — add triage. <b style={{color:V("red")}}>Bottom-left</b>:
          neither holds — the label is covering more than one kind of work.
        </div>
        <label style={{ display:"flex", alignItems:"center", gap:7, marginTop:10, cursor:"pointer" }}>
          <input type="checkbox" checked={hideShaky} onChange={e=>setHideShaky(e.target.checked)} style={{ accentColor:V("accent") }} />
          <span style={{ fontSize:10.5, color:V("label") }}>Hide {nShaky} low-confidence workflows (≥{UNRELIABLE}% no action extracted)</span>
        </label>
      </div>

      <div style={{ flex:1, minWidth:0, display:"flex", flexDirection:"column" }}>
        <SectionLabel>Ranking</SectionLabel>
        <div style={{ display:"flex", gap:5, marginBottom:8 }}>
          {[["score","Score"],["jac","Similarity"],["ent","Entropy"],["bimod","Bimodality"],["n","Volume"]].map(([k,l])=>(
            <button key={k} onClick={()=>setSortBy(k)} style={{ fontSize:10.5, padding:"3px 9px", borderRadius:5, border:"none", cursor:"pointer", fontWeight:700, background: sortBy===k ? V("accent") : V("inactive"), color: sortBy===k ? V("onaccent") : V("label") }}>{l}</button>
          ))}
        </div>
        <div style={{ background:V("subtle"), border:`1px solid ${V("border")}`, borderRadius:7, padding:"8px 10px", marginBottom:8, display:"flex", flexDirection:"column", gap:3 }}>
          {[
            ["score", "composite 0-100 — higher = more repeatable", V("text3")],
            ["jac",   "step overlap between two random tickets — higher = more alike", V("green")],
            ["ent",   "route spread — 0 all identical, 1 all unique", V("text3")],
            ["bimod", "two distinct groups? — above 0.45 = split candidate", V("orange")],
            ["cv",    "hours variation (from the effort data, not the steps)", V("blue")],
          ].map(([k, d, c]) => (
            <div key={k} style={{ display:"flex", gap:8, fontSize:9.5, lineHeight:1.4 }}>
              <span style={{ color:c, fontWeight:700, width:38, flexShrink:0, textTransform:"uppercase", letterSpacing:".04em" }}>{k}</span>
              <span style={{ color:V("label") }}>{d}</span>
            </div>
          ))}
        </div>
        <div style={{ flex:1, overflowY:"auto", display:"flex", flexDirection:"column", gap:3, paddingRight:4 }}>
          {ranked.map((it,i) => (
            <div key={it.wf} onClick={()=>onOpenWorkflow(it.wf)} onMouseEnter={()=>setHover(it.wf)} onMouseLeave={()=>setHover(null)}
              style={{ display:"flex", alignItems:"center", gap:8, padding:"5px 8px", borderRadius:7, cursor:"pointer", background: hover===it.wf ? V("sel") : "transparent" }}>
              <div style={{ width:18, fontSize:10, color:V("desc"), textAlign:"right", flexShrink:0 }}>{i+1}</div>
              <div style={{ width:32, textAlign:"center", flexShrink:0 }}>
                <span style={{ fontSize:13, fontWeight:800, color:procColor(it.pr.score) }}>{it.pr.score}</span>
              </div>
              <div style={{ flex:1, minWidth:0 }}>
                <div style={{ fontSize:11.5, color:V("text2"), overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }} title={it.wf}>
                  {it.wf} <ShakyBadge pr={it.pr} compact />
                </div>
                <div style={{ fontSize:9, color:V("desc") }}>{it.cat} · {it.pr.npat} patterns · {it.g.n.toLocaleString()} tix</div>
              </div>
              <div style={{ display:"flex", gap:6, flexShrink:0, alignItems:"center" }}>
                <Mini label="jac"   value={it.pr.jac.toFixed(2)} color={jacColor(it.pr.jac)} />
                <Mini label="ent"   value={it.pr.ent.toFixed(2)} color={V("text3")} />
                <Mini label="bimod" value={it.pr.bimod.toFixed(2)} color={it.pr.bimod>=0.45?V("orange"):V("label")} />
                <Mini label="cv"    value={it.g.cv.toFixed(2)} color={cvColor(it.g.cv)} />
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

function Mini({ label, value, color }) {
  return (
    <div style={{ textAlign:"right", width:40 }}>
      <div style={{ fontSize:8, color:V("desc"), textTransform:"uppercase", letterSpacing:".05em" }}>{label}</div>
      <div style={{ fontSize:11, fontWeight:700, color }}>{value}</div>
    </div>
  );
}
function Chip({ active, onClick, label }) {
  return <button onClick={onClick} style={{ padding:"3px 11px", borderRadius:5, border:"none", cursor:"pointer", fontSize:11, fontWeight:600, background: active ? V("accent") : V("inactive"), color: active ? V("onaccent") : V("label") }}>{label}</button>;
}
function SectionLabel({ children, top }) {
  return <div style={{ fontSize:10, color:V("label"), fontWeight:700, margin: top ? "17px 0 8px" : "0 0 8px", textTransform:"uppercase", letterSpacing:".08em" }}>{children}</div>;
}
function Legend() {
  return (
    <div style={{ display:"flex", gap:13, marginTop:7, flexWrap:"wrap" }}>
      {BK.map(k => <span key={k} style={{ fontSize:10.5, color:V("label") }}><span style={{ display:"inline-block", width:10, height:10, borderRadius:2, background:BK_COLOR[k], marginRight:4, verticalAlign:"-1px" }} />{BK_LABEL[k]}</span>)}
    </div>
  );
}

export default function App() {
  const [theme, setTheme] = useState("light");
  const [mode, setMode] = useState("workflow");
  const [selWf, setSelWf] = useState(DATA.wf_order.slice().sort((a,b)=>DATA.workflows[b].g.n-DATA.workflows[a].g.n)[0]);
  const [selCo, setSelCo] = useState(DATA.companies[0]);
  const [search, setSearch] = useState("");
  const [fCat, setFCat] = useState("All");
  const [fCx, setFCx] = useState("All");
  const [sortBy, setSortBy] = useState("n");
  const cats = Object.keys(DATA.cats).sort();

  const wfList = useMemo(() => DATA.wf_order
    .filter(wf => fCat==="All" || DATA.workflows[wf].cat===fCat)
    .filter(wf => fCx==="All" || DATA.workflows[wf].cx===fCx)
    .filter(wf => !search || wf.toLowerCase().includes(search.toLowerCase()))
    .map(wf => ({ wf, ...DATA.workflows[wf], g:DATA.workflows[wf].g }))
    .sort((a,b) => sortBy==="n" ? b.g.n-a.g.n : sortBy==="aht" ? b.g.aht-a.g.aht : sortBy==="cv" ? b.g.cv-a.g.cv
                 : sortBy==="tail" ? b.g.tail-a.g.tail : sortBy==="proc" ? (b.pr?.score||0)-(a.pr?.score||0)
                 : b.g.frr-a.g.frr),
  [fCat, fCx, search, sortBy]);

  const openWorkflow = (wf) => { setSelWf(wf); setMode("workflow"); };

  return (
    <div style={{ ...THEMES[theme], fontFamily:"'Inter',system-ui,sans-serif", background:V("bg"), minHeight:"100vh", color:V("text2") }}>
      <div style={{ borderBottom:`1px solid ${V("border")}`, padding:"12px 22px", display:"flex", alignItems:"center", justifyContent:"space-between", background:V("card") }}>
        <div>
          <span style={{ fontSize:17, fontWeight:800, color:V("text"), letterSpacing:"-.02em" }}>Distributional Shape Explorer</span>
          <span style={{ fontSize:11.5, color:V("faint"), marginLeft:12 }}>73 workflows · 15 companies · 144,703 tickets · steps extracted from 144,631</span>
        </div>
        <div style={{ display:"flex", alignItems:"center", gap:10 }}>
        <button onClick={()=>setTheme(theme==="light"?"dark":"light")}
          title={theme==="light" ? "Switch to dark mode" : "Switch to light mode"}
          style={{ display:"flex", alignItems:"center", gap:6, padding:"5px 11px", borderRadius:7,
            border:`1px solid ${V("border")}`, background:V("inactive"), color:V("label"),
            fontSize:11.5, fontWeight:700, cursor:"pointer" }}>
          <span style={{ fontSize:13, lineHeight:1 }}>{theme==="light" ? "\u25D1" : "\u25D0"}</span>
          {theme==="light" ? "Dark" : "Light"}
        </button>
        <div style={{ display:"flex", background:V("inactive"), borderRadius:7, padding:3 }}>
          {[["workflow","By workflow"],["company","By company"],["process","Process"]].map(([m,l])=>(
            <button key={m} onClick={()=>setMode(m)} style={{ padding:"5px 15px", borderRadius:5, border:"none", cursor:"pointer", fontSize:12, fontWeight:700, background: mode===m ? V("accent") : "transparent", color: mode===m ? V("onaccent") : V("label") }}>{l}</button>
          ))}
        </div>
      </div>
      </div>

      <div style={{ display:"flex", height:"calc(100vh - 55px)" }}>
        {mode !== "process" && (
          <div style={{ width:mode==="workflow"?395:230, borderRight:`1px solid ${V("border")}`, display:"flex", flexDirection:"column", flexShrink:0 }}>
            {mode==="workflow" ? (
              <>
                <div style={{ padding:"11px 13px", borderBottom:`1px solid ${V("border")}`, background:V("card") }}>
                  <input placeholder="Search workflows…" value={search} onChange={e=>setSearch(e.target.value)}
                    style={{ width:"100%", background:V("card"), border:`1px solid ${V("border")}`, borderRadius:6, padding:"6px 10px", color:V("text2"), fontSize:13, boxSizing:"border-box", marginBottom:8, outline:"none" }} />
                  <div style={{ display:"flex", gap:5 }}>
                    <select value={fCat} onChange={e=>setFCat(e.target.value)} style={selStyle}><option>All</option>{cats.map(c=><option key={c}>{c}</option>)}</select>
                    <select value={fCx} onChange={e=>setFCx(e.target.value)} style={{...selStyle,width:72}}><option>All</option><option>low</option><option>medium</option><option>high</option></select>
                    <select value={sortBy} onChange={e=>setSortBy(e.target.value)} style={{...selStyle,width:92}}>
                      <option value="n">↓ Volume</option><option value="aht">↓ AHT</option><option value="cv">↓ CV</option><option value="tail">↓ Tail</option><option value="proc">↓ Process</option><option value="frr">↑ FRR</option>
                    </select>
                  </div>
                  <div style={{ fontSize:9.5, color:V("faint"), marginTop:6 }}>{wfList.length} workflows</div>
                </div>
                <div style={{ flex:1, overflowY:"auto", padding:"8px 9px", display:"flex", flexDirection:"column", gap:5 }}>
                  {wfList.map(({wf,cx,cat,g,pr})=>(
                    <div key={wf} onClick={()=>setSelWf(wf)} style={{ background: selWf===wf ? V("sel") : V("card"), border: selWf===wf ? `1.5px solid ${V("accent2")}` : `1.5px solid ${V("border")}`, borderRadius:9, padding:"10px 12px", cursor:"pointer" }}>
                      <div style={{ display:"flex", justifyContent:"space-between", gap:8, marginBottom:7 }}>
                        <div style={{ flex:1 }}>
                          <span style={{ fontSize:9, background:CX_BG[cx], color:CX_COLOR[cx], borderRadius:3, padding:"1px 5px", fontWeight:800 }}>{(cx||"—").toUpperCase()}</span>
                          <span style={{ fontSize:10, color:V("desc"), marginLeft:5 }}>{cat}</span>
                          <div style={{ fontSize:12.5, fontWeight:600, color:V("text2"), marginTop:3, lineHeight:1.25 }}>{wf} <ShakyBadge pr={pr} compact /></div>
                        </div>
                        <span style={{ fontSize:10.5, color:V("desc") }}>{g.n.toLocaleString()}</span>
                      </div>
                      <StackBar b={g.b} height={18} />
                      <div style={{ display:"flex", gap:11, marginTop:7, flexWrap:"wrap" }}>
                        <span style={{ fontSize:10.5, color:V("label") }}>AHT <b style={{color:V("text3")}}>{g.aht}m</b></span>
                        <span style={{ fontSize:10.5, color:V("label") }}>FRR <b style={{color:V("text3")}}>{g.frr}%</b></span>
                        <span style={{ fontSize:10.5, color:V("label") }}>CV <b style={{color:cvColor(g.cv)}}>{g.cv.toFixed(2)}</b></span>
                        {pr && <span style={{ fontSize:10.5, color:V("label") }}>proc <b style={{color:procColor(pr.score)}}>{pr.score}</b></span>}
                      </div>
                    </div>
                  ))}
                </div>
              </>
            ) : (
              <div style={{ flex:1, overflowY:"auto", padding:"10px 9px", display:"flex", flexDirection:"column", gap:5 }}>
                {DATA.companies.map(c=>{
                  const sm = DATA.company_summary[c];
                  return (
                    <div key={c} onClick={()=>setSelCo(c)} style={{ background: selCo===c ? V("sel") : V("card"), border: selCo===c ? `1.5px solid ${V("accent2")}` : `1.5px solid ${V("border")}`, borderRadius:9, padding:"10px 12px", cursor:"pointer" }}>
                      <div style={{ display:"flex", justifyContent:"space-between", marginBottom:7 }}>
                        <span style={{ fontSize:13, fontWeight:700, color:V("text2") }}>{c}</span>
                        <span style={{ fontSize:10.5, color:V("desc") }}>{sm.n.toLocaleString()}</span>
                      </div>
                      <StackBar b={sm.b} height={16} />
                      <div style={{ display:"flex", gap:12, marginTop:6 }}>
                        <span style={{ fontSize:10, color:V("label") }}>AHT <b style={{color:V("text3")}}>{sm.aht}m</b></span>
                        <span style={{ fontSize:10, color:V("label") }}>FRR <b style={{color:V("text3")}}>{sm.frr}%</b></span>
                        <span style={{ fontSize:10, color:V("label") }}>CV <b style={{color:cvColor(sm.cv)}}>{sm.cv.toFixed(2)}</b></span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        )}
        <div style={{ flex:1, overflowY:"auto", padding:22 }}>
          {mode==="workflow" && <WorkflowDetail wf={selWf} />}
          {mode==="company"  && <CompanyDetail company={selCo} />}
          {mode==="process"  && <ProcessView onOpenWorkflow={openWorkflow} />}
        </div>
      </div>
    </div>
  );
}

const selStyle = { background:V("card"), border:`1px solid ${V("border")}`, borderRadius:6, padding:"4px 7px", color:V("label"), fontSize:10.5, flex:1, outline:"none" };
'''
