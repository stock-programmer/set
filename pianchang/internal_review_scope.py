"""Select internal cuts whose original timing, selected footage and dispatch state can be verified."""
from pathlib import Path

from pianchang.acceptance import file_sha256
from pianchang.content_review import VERSION, NORMALIZATION_VERSION, _asset, _read, internal_cut_windows
from pianchang.legacy_sound_review import sidecar_commission
from pianchang.story_spec import digest_json


def _timing(base, refs, shot, contract, clip):
    from pianchang.content_review import CONTENT_KINDS
    matches = [ref for ref in refs if Path(ref["path"]).resolve().parent.parent == base/"model"]
    if len(matches) != 1:
        return None
    ref = matches[0]
    path = Path(ref["path"]).resolve()
    receipt = _read(path)
    call_path = path.with_name("call.json")
    call = _read(call_path)
    req = call["request"]
    video = receipt["video"]
    criteria = [c for c in contract["requirements"] if c["kind"] in CONTENT_KINDS and
                c["kind"] not in {"sequence_review","on_screen_text","internal_cut"}]
    context = {"shot_id":shot["id"],"shot":shot,"expected_state_before":contract["expected_state_before"],
               "expected_state_after":contract["expected_state_after"],"entities":contract["entity_snapshots"]}
    if (path.name != "receipt-p4.json" or digest_json(receipt) != ref["sha256"]
            or Path(receipt["call_path"]).resolve() != call_path or digest_json(call) != receipt["call_sha256"]
            or call.get("status") != "completed" or receipt.get("execution_status") != "executed"
            or not call.get("response") or call["response"] != receipt.get("response")
            or digest_json(req) != call["input_sha256"] or call["input_sha256"] != receipt["request_sha256"]
            or path.parent.name != call["input_sha256"][:12] or req.get("audiovisual") is not False
            or receipt["context"] != context or req.get("context") != context or criteria != req["criteria"]
            or criteria != receipt["criteria"] or req["references"] != receipt["references"] or req["video"] != video
            or video["binding"]["source"] != _asset(clip["media_path"])
            or video["binding"]["source"]["sha256"] != clip["media_sha256"]
            or not Path(video["path"]).resolve().is_relative_to(base/"input") or file_sha256(video["path"]) != video["sha256"]):
        return None
    return ref,receipt


def _cut_references(base, shot):
    refs = []
    pins = {(p["asset_id"],p["revision_id"]):p for p in shot.get("required_refs",[])}
    for i,pin in enumerate(pins.values()):
        target = base/"internal-refs"/(pin["media_sha256"][:16]+(".wav" if pin["purpose"]=="voice" else ".jpg"))
        receipt = _read(target.with_suffix(".json"))
        if (Path(receipt["path"]).resolve() != target.resolve() or receipt["source_sha256"] != pin["media_sha256"] or
                file_sha256(receipt["source_path"]) != pin["media_sha256"] or file_sha256(target) != receipt["sha256"]):
            raise ValueError("现有切点参考与冻结素材不匹配。")
        refs.append({**receipt,"id":"R"+str(i),"pin":pin})
    return refs


def _cached_cut(folder, fingerprint, cid, selected_ref=None):
    """Both raw provider observations must be complete and from the exact ordered split call."""
    from pianchang.openai_review import merge_cut
    request_sha = digest_json(fingerprint)
    directory = folder/request_sha[:12]
    if not directory.is_dir() or directory.is_symlink() or set(folder.iterdir()) != {directory}:
        return None
    known = {"audio-call.json","visual-call.json","audio-call.lock","visual-call.lock","call.json",
             "receipt-p4.json","receipt-cut-assembly-v2.json"}
    if any(p.name not in known or p.is_symlink() or not p.is_file() for p in directory.iterdir()):
        return None
    receipt = directory/"receipt-p4.json"
    assembly = directory/"receipt-cut-assembly-v2.json"
    audio, visual = (_read(directory/name) for name in ("audio-call.json","visual-call.json"))
    expected_visual = {**fingerprint,"audio_sha256":digest_json(audio["response"])}
    for record,expected in ((audio,fingerprint), (visual,expected_visual)):
        response = record.get("response") or {}
        rows = (response.get("data") or {}).get("observations") or []
        if (record.get("status") != "completed" or record.get("request") != expected
                or record.get("input_sha256") != digest_json(expected) or not response.get("request_id")
                or len(rows) != 1 or rows[0].get("criterion_id") != cid or not rows[0].get("evidence")
                or rows[0].get("verdict") not in {"PASS","FAIL","UNKNOWN"}):
            return None
    cut = fingerprint["context"]["cut_s"]
    sound = audio["response"]["data"]["observations"][0]["evidence"]
    if (any(e.get("asset_id")!="A0" or not 0<=e["start_s"]<e["end_s"]<=fingerprint["video"]["duration_s"] for e in sound)
            or min(e["start_s"] for e in sound)>=cut or max(e["end_s"] for e in sound)<=cut):
        return None
    merged = merge_cut(visual["response"],audio["response"],cut)
    parts = {"audio-call.json":digest_json(audio),"visual-call.json":digest_json(visual)}
    aggregate = {"request":fingerprint,"input_sha256":request_sha,"status":"completed","response":merged}
    call_path = directory/"call.json"
    if call_path.exists() and _read(call_path)!=aggregate:
        return None
    if not receipt.exists():
        if selected_ref or assembly.exists():
            return None
        return {"mode":"assemble_parts","folder":str(folder),"parts":parts}
    raw = _read(receipt)
    if (raw.get("version")!=VERSION or raw.get("request_sha256") != request_sha or raw.get("context") != fingerprint["context"]
            or raw.get("video") != fingerprint["video"] or raw.get("references") != fingerprint["references"]
            or raw.get("criteria") != fingerprint["criteria"] or [r["criterion_id"] for r in raw["rows"]] != [cid]):
        return None
    if selected_ref and (Path(selected_ref["path"]).resolve() != receipt or selected_ref["sha256"] != digest_json(raw)):
        return None
    chosen = assembly if assembly.is_file() else receipt
    result = _read(chosen) if chosen != receipt else raw
    mode = "assemble"
    if result.get("execution_status") == "executed":
        call = _read(directory/"call.json")
        if chosen==assembly and result.get("normalized_from")!={"path":str(receipt),"sha256":digest_json(raw)}:
            return None
        if (result.get("version")!=VERSION or result.get("normalization_version")!=NORMALIZATION_VERSION or
                result.get("request_sha256") != request_sha or call.get("request") != fingerprint or call.get("status") != "completed"
                or call.get("input_sha256") != request_sha or digest_json(call) != result.get("call_sha256")
                or Path(result["call_path"]).resolve() != directory/"call.json" or call.get("response") != result.get("response") or call["response"]!=merged
                or any(result.get(k)!=fingerprint[k] for k in ("context","video","references","criteria"))
                or [r.get("criterion_id") for r in result["rows"]] != [cid] or result["rows"][0].get("execution_status") != "executed"):
            return None
        mode = "cached"
    elif chosen != receipt or raw.get("execution_status") not in {"execution_error","partial"}:
        return None
    return {"mode":mode,"folder":str(folder),"receipt":{"path":str(chosen),"sha256":digest_json(result)},
            "parts":parts}


def _cut_mode(config, base, shot, clip, timing, criterion, refs, current_row, selected_row, remaining):
    from pianchang.content_review import internal_cut_context, normalize_observations, review_fingerprint, VisualCutObserver
    cid = criterion["criterion_id"]
    pair = criterion["creative_pair"]
    members = [member["id"] for member in shot["creative_shots"]]
    window = internal_cut_windows(pair,timing["rows"])
    if (pair not in [members[i:i+2] for i in range(len(members)-1)] or cid != "internal:"+"->".join(pair) or not window):
        return None
    model_rows = normalize_observations(timing["response"],timing["video"],timing["references"],timing["criteria"],timing["context"])
    if internal_cut_windows(pair,model_rows) != window:
        return None
    folder = base/("internal-"+digest_json(pair)[:16])
    if folder.is_symlink():
        return None
    attached = [r for r in refs if Path(r["path"]).resolve().parent.parent == folder]
    status = current_row.get("execution_status")
    if status=="not_run":
        if attached or selected_row:
            return None
    elif status not in {"execution_error","partial"} or len(attached)!=1 or not selected_row or selected_row.get("execution_status")!=status:
        return None
    if not folder.exists():
        if status=="not_run" and remaining>=2 and config.api_key and config.continuity_config.api_key:
            return {"mode":"new","folder":str(folder)}
        return None
    if not folder.is_dir() or not any(p.is_dir() for p in folder.iterdir()):
        return None
    wav = base/("actual-"+clip["media_sha256"]+".wav")
    audio = _read(wav.with_suffix(".json"))
    if (audio.get("sha256")!=file_sha256(wav) or Path(audio["path"]).resolve()!=wav or
            audio["source_sha256"]!=clip["media_sha256"] or Path(audio["source_path"]).resolve()!=Path(clip["media_path"]).resolve()):
        return None
    context = internal_cut_context(shot, _asset(clip["media_path"]),timing,criterion,audio)
    fingerprint = review_fingerprint(config,timing["video"],_cut_references(base,shot),[{**criterion,"kind":"sequence_review"}],
        context,audiovisual=True,split_audio=True,prompt=VisualCutObserver.system_prompt)
    return _cached_cut(folder,fingerprint,cid,attached[0] if attached else None)


def internal_cut_scope(service, view, snapshot, owner, work, reports, remaining, *, excluding=None):
    from pianchang.content_review import VisualCutObserver
    from pianchang.openai_review import AUDIO_CUT_PROMPT
    from pianchang.shot_approval import active_production
    cid, pid = snapshot["request_id"],view["project_id"]
    if (not reports or not snapshot.get("retake_stop") or snapshot.get("active") or snapshot.get("unknown_shots") or snapshot.get("failed_shots")
            or not view.get("spec") or not view.get("script_ref") or view.get("pending_script_ref")
            or view["spec"]!=snapshot.get("spec_ref") or view["script_ref"]!=snapshot.get("script_ref")
            or snapshot["project_sha256"]!=digest_json(view["project"])):
        return None
    with service.lock:
        if active_production([j for j in service.state["jobs"] if j["id"]!=excluding],pid,cid):
            return None
    driver = sidecar_commission(service.root,pid,cid,snapshot["job_id"])
    if not driver or owner["kind"]=="pi_video" and owner["id"] not in driver["submitted"]:
        return None
    cfg = service.vision_config
    audio = getattr(cfg,"continuity_config",None)
    if not audio or audio.adapter!="openai":
        return None
    clips = {sid:item["clip"] for sid,item in snapshot["available"].items()}
    recorded = _read(work/"reports.json")
    media = {s:(clip["media_sha256"],clip["media_path"]) for s,clip in clips.items()}
    selected = {}
    for shot in view["project"]["shots"]:
        sid = shot["id"]
        if sid not in reports or sid not in clips:
            continue
        report_sha,refs,unresolved = reports[sid]
        if not any(k.startswith("internal:") for k in unresolved):
            continue
        ref = recorded[sid]
        directory = Path(ref["report_path"]).resolve().parent
        base = Path(ref.get("content_work_path") or directory.parent).resolve()
        root = (work/"cv2"/sid).resolve()
        clip = clips[sid]
        if base not in {root,root/("candidate-"+clip["media_sha256"][:16])} or not directory.is_relative_to(root):
            continue
        report = _read(directory/"report.json")
        contract,evidence = (_read(directory/name) for name in ("contract.json","content.json"))
        inspection = _read(ref["inspection_path"])
        if (report_sha!=digest_json(report) or ref["report_sha256"]!=report_sha
                or digest_json(inspection)!=report["inspection_sha256"] or inspection["media_sha256"]!=clip["media_sha256"]
                or digest_json(contract)!=report["contract_sha256"] or contract["shot_sha256"]!=digest_json(shot)
                or report["content_evidence_sha256"]!=digest_json(evidence) or evidence["receipts"]!=refs
                or report["media_sha256"]!=clip["media_sha256"] or report["project_sha256"]!=snapshot["project_sha256"]):
            continue
        if {s:(v["sha256"],v["path"]) for s,v in evidence.get("selected_media",{}).items()} != media:
            return None
        try:
            main = _timing(base,refs,shot,contract,clip)
        except (OSError,ValueError,KeyError,TypeError):
            continue
        if not main:
            continue
        timing_ref,timing = main
        chosen = {}
        for criterion in contract["requirements"]:
            key = criterion["criterion_id"]
            if criterion.get("kind")!="internal_cut" or not criterion["required"] or key not in unresolved:
                continue
            rows = [r for r in report["results"] if r["criterion_id"]==key and r["required"] and r["status"]=="UNKNOWN"]
            existing = [r for r in evidence["rows"] if r["criterion_id"]==key]
            if len(rows)!=1 or len(existing)>1:
                continue
            try:
                decision = _cut_mode(cfg,base,shot,clip,timing,criterion,refs,rows[0],next(iter(existing),None),remaining)
            except (OSError,ValueError,TypeError,KeyError):
                continue
            if decision:
                chosen[key] = decision
                if decision["mode"]=="new":
                    remaining -= 2
        if chosen:
            selected[sid] = {"media_sha256":clip["media_sha256"],"report":report_sha,"timing":timing_ref,"criteria":chosen}
    if not selected:
        return None
    # A pending network probe must never delay recovery from complete local observations.
    local = {}
    for sid, item in selected.items():
        cuts = {key:v for key,v in item["criteria"].items() if v["mode"]!="new"}
        if cuts:
            local[sid] = {**item,"criteria":cuts}
    needs_route = not local
    selected = local or selected
    routes = [[c.model,c.base_url,getattr(c,"route_account","")] for c in (cfg,audio)]
    identity = ["internal-cut-review/1",VERSION,NORMALIZATION_VERSION,digest_json(VisualCutObserver.system_prompt),
                digest_json(AUDIO_CUT_PROMPT),cid,snapshot["sha256"],routes,selected]
    return {"internal_cuts":selected,"criteria":{sid:sorted(s["criteria"]) for sid,s in selected.items()},
            "allowed":{},"token":digest_json([owner["id"],identity]),"key":"review-internal-cut-"+digest_json(identity),
            "urls":sorted({c.base_url.rstrip("/")+"/chat/completions" for c in (cfg,audio)}) if needs_route else []}
