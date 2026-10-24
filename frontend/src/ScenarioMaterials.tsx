import { useEffect, useRef, useState } from "react";
import { Trash2 } from "lucide-react";
import { api, apiSource, errorText } from "./api";
import { Field, ErrorBox, Modal, useResource } from "./ui";
import "./scenarioMaterials.css";
export const MAX_SCENARIO_MATERIALS = 10;
export function appendMaterials(current: string[], added: string[]) {
  const next = [...new Set([...current, ...added])];
  if (next.length > MAX_SCENARIO_MATERIALS) throw new Error("Не больше 10 материалов в сообщении");
  return next;
}
export function ScenarioMaterials({assetIds, locked, onChange}: { assetIds: string[]; locked: boolean; onChange: (ids: string[]) => void; }) {
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [gallery, setGallery] = useState(false);
  const current = useRef(assetIds); current.current = assetIds;
  const latestOnChange = useRef(onChange); latestOnChange.current = onChange;
  const editable = useRef(!locked); editable.current = !locked;
  const mounted = useRef(false);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  function apply(ids: string[]) {
    if (mounted.current && editable.current) latestOnChange.current(appendMaterials(current.current, ids));
  }
  async function upload(files: File[]) {
    if (!files.length) return;
    if (current.current.length + files.length > MAX_SCENARIO_MATERIALS) {setError("Не больше 10 материалов в сообщении"); return;}
    setBusy(true); setError("");
    const added: string[] = [], failures: string[] = [];
    for (const file of files) {
      if (!mounted.current || !editable.current) return;
      if (file.size > 50 * 1024 * 1024) { failures.push(`${file.name}: не больше 50 МБ`); continue; }
      const data = new FormData(); data.append("file", file); data.append("purpose", "SCENARIO_MATERIAL");
      try { const response = await api.upload("/assets", data); added.push(response.data.asset_id || response.data.id); }
      catch(e) { failures.push(`${file.name}: ${errorText(e)}`); }
    }
    if (!mounted.current || !editable.current) return;
    try {if (added.length) apply(added);}
    catch(e) {failures.push(errorText(e));}
    setError(failures.join("; ")); setBusy(false);
  }
  return <section><h4>Материалы сообщения</h4>
    {assetIds.map(id => <AssetChip key={id} id={id} locked={locked || busy} onRemove={() => onChange(assetIds.filter(x => x !== id))} />)}
    {!locked && <><Field label={busy ? "Материалы загружаются…" : "Прикрепить файлы"} hint="До 10 материалов, каждый до 50 МБ.">
      <input type="file" multiple disabled={busy} accept=".pdf,.pptx,.png,.jpg,.jpeg,.txt" onChange={e => { const files = Array.from(e.target.files || []); e.target.value = ""; void upload(files); }} />
    </Field><button type="button" className="button secondary" disabled={busy || assetIds.length >= 10} onClick={() => setGallery(true)}>Галерея Корсика</button></>}
    <ErrorBox error={error} />
    {gallery && <MaterialGallery selected={assetIds} onClose={() => setGallery(false)} onAdd={ids => { apply(ids); if (mounted.current) setGallery(false); }} />}
  </section>;
}
function AssetChip({id, locked, onRemove}: {id:string; locked:boolean; onRemove:()=>void}) {
  const asset = useResource(`/assets/${id}`);
  return <div className="asset-chip"><a href={apiSource(asset.data?.source_url, `/api/v1/assets/${id}/content`)} target="_blank" rel="noreferrer">{asset.data?.filename || "Прикреплённый материал"}</a>{!locked && <button type="button" className="icon-button" aria-label="Убрать материал" onClick={onRemove}><Trash2 size={13}/></button>}</div>;
}
function MaterialGallery({selected, onClose, onAdd}: {selected:string[];onClose:()=>void;onAdd:(ids:string[])=>void}) {
  const resource = useResource("/scenario-materials/images");
  const [chosen, setChosen] = useState<string[]>([]), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const mounted = useRef(false);
  useEffect(() => {mounted.current = true; return () => {mounted.current = false;};}, []);
  const rows = [...(resource.data?.builtins || []).map((r:any) => ({...r, key:`builtin:${r.id}`, builtin:true})), ...(resource.data?.items || []).map((r:any) => ({...r,key:r.id}))];
  async function add() {
    setBusy(true);setError("");
    try {
      const ids:string[]=[];
      for(const key of chosen) {
        if (!mounted.current) return;
        if(key.startsWith("builtin:")) {const response=await api.post("/scenario-materials/builtin", {id:key.slice(8)});ids.push(response.data.asset_id || response.data.id);}
        else ids.push(key);
      }
      if (mounted.current) onAdd(ids);
    } catch(e) {if (mounted.current) setError(errorText(e));} finally {if (mounted.current) setBusy(false);}
  }
  return <Modal title="Галерея Корсика" onClose={onClose}><ErrorBox error={error || resource.error}/>
    {resource.loading && <p>Загрузка…</p>}
    <div className="scenario-material-gallery">{rows.map((row:any) => <label key={row.key} className={chosen.includes(row.key) ? "selected" : ""}>
      <input type="checkbox" checked={chosen.includes(row.key) || selected.includes(row.key)} disabled={busy || selected.includes(row.key) || (!chosen.includes(row.key) && selected.length + chosen.length >= 10)} onChange={e => setChosen(e.target.checked ? [...chosen,row.key] : chosen.filter(k => k!==row.key))}/>
      <img src={row.builtin ? row.source_url : apiSource(row.source_url, `/api/v1/assets/${row.id}/content`)} alt={row.filename}/><span>{row.filename}</span>
    </label>)}</div>
    <div className="button-row"><button type="button" className="button secondary" disabled={busy} onClick={onClose}>Отмена</button><button type="button" className="button" disabled={busy || !chosen.length} onClick={add}>Добавить выбранные</button></div>
  </Modal>;
}
