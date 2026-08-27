"""
disaster_survey.py
===================
災害調査写真管理システム — メイン処理スクリプト

【処理フロー】
1. 対象フォルダ（BOXマウント or ローカル）をスキャン
2. 写真をEXIF解析（GPS座標・撮影日時・端末情報）
3. 区切り写真を検出（No紙OCR or 手のひら → 工区を分割）
4. Claude API で写真内容を読み取り（AI推定工種・所見）
5. 配置図HTML を自動生成
6. Notion DB（災害調査箇所DB）へ登録

【セットアップ】
    conda activate myenv   （または任意のvenv）
    pip install pillow requests

【実行方法】
    python disaster_survey.py --folder "C:/BOX/業者A" --vendor "業者A名"
    python disaster_survey.py --folder "C:/BOX/業者A" --vendor "業者A名" --dry-run

【必須設定】
    下記 CONFIG セクションの NOTION_TOKEN と ANTHROPIC_API_KEY を設定してください。
"""

import os
import re
import sys
import json
import base64
import argparse
import time
import configparser
import subprocess
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

# Windows ターミナルで日本語・記号を正しく出力するため UTF-8 に統一
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ============================================================
# CONFIG — config.ini から読み込み
# ============================================================
# APIキーやDB IDは config.ini に記載してください。
# スクリプトを上書きしても config.ini は影響を受けません。
# 他のPCに移すときは config.ini だけ用意すればOKです。
# ============================================================

def load_config() -> configparser.ConfigParser:
    """スクリプトと同じフォルダの config.ini を読み込む。"""
    script_dir = Path(__file__).resolve().parent
    config_path = script_dir / "config.ini"

    if not config_path.exists():
        # config.ini が無ければサンプルを自動生成して終了
        sample = configparser.ConfigParser()
        sample["API"] = {
            "notion_token": "ここにNotionトークンを貼り付け",
            "anthropic_api_key": "ここにAnthropic APIキーを貼り付け",
        }
        sample["NOTION"] = {
            "place_db_id": "55e3edc4-1e6a-4a4a-9681-84169fad6f6d",
            "photo_db_id": "36f4880e-37de-405c-a719-8132bad1eb67",
        }
        sample["OPTIONS"] = {
            "map_output_dir": "",
            "divider_mode": "both",
        }
        with open(config_path, "w", encoding="utf-8") as f:
            sample.write(f)
        print("=" * 60)
        print("  config.ini を新規作成しました。")
        print(f"  場所: {config_path}")
        print("  メモ帳で開いて、NotionトークンとAPIキーを設定してから")
        print("  もう一度実行してください。")
        print("=" * 60)
        sys.exit(0)

    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf-8")
    return config

_cfg = load_config()

NOTION_TOKEN      = _cfg.get("API", "notion_token", fallback="")
ANTHROPIC_API_KEY = _cfg.get("API", "anthropic_api_key", fallback="")

# Notion: 災害調査箇所DB / 写真DB のID
NOTION_DS_ID = _cfg.get("NOTION", "place_db_id", fallback="55e3edc4-1e6a-4a4a-9681-84169fad6f6d")

# 配置図HTMLの出力先（空文字 = スクリプトと同じフォルダ）
MAP_OUTPUT_DIR = _cfg.get("OPTIONS", "map_output_dir", fallback="")

# GitHub Pages 自動アップロード設定
GITHUB_ENABLED  = _cfg.get("GITHUB", "enabled", fallback="no").lower() in ("yes", "true", "1")
GITHUB_REPO_DIR = _cfg.get("GITHUB", "repo_dir", fallback="")
GITHUB_PAGES_URL = _cfg.get("GITHUB", "pages_url", fallback="")

# 区切り写真の判定ルール
DIVIDER_MODE = _cfg.get("OPTIONS", "divider_mode", fallback="both")

# APIキー未設定チェック
if not NOTION_TOKEN or "貼り付け" in NOTION_TOKEN:
    print("[ERROR] config.ini の notion_token が未設定です。メモ帳で設定してください。")
    sys.exit(1)
if not ANTHROPIC_API_KEY or "貼り付け" in ANTHROPIC_API_KEY:
    print("[ERROR] config.ini の anthropic_api_key が未設定です。メモ帳で設定してください。")
    sys.exit(1)

# 対応画像拡張子
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".heic", ".heif"}

# JST オフセット
JST = timezone(timedelta(hours=9))

# ============================================================
# EXIF ユーティリティ
# ============================================================

def _to_float(v):
    try:
        return float(v)
    except Exception:
        return 0.0

def _dms_to_dd(dms, ref):
    d, m, s = [_to_float(x) for x in dms]
    dd = d + m / 60 + s / 3600
    if ref in ("S", "W"):
        dd = -dd
    return round(dd, 7)

def extract_exif(path: Path) -> dict:
    """画像ファイルから EXIF メタデータを取得して辞書で返す。"""
    result = {
        "file":     path.name,
        "path":     str(path),
        "lat":      None,
        "lon":      None,
        "alt":      None,
        "datetime": None,
        "make":     None,
        "model":    None,
    }
    try:
        img = Image.open(path)
        raw = img._getexif()
        if not raw:
            return result
        for tag_id, val in raw.items():
            tag = TAGS.get(tag_id, tag_id)
            if tag == "GPSInfo":
                g = {GPSTAGS.get(k, k): v for k, v in val.items()}
                result["lat"] = _dms_to_dd(g["GPSLatitude"],  g.get("GPSLatitudeRef",  "N"))
                result["lon"] = _dms_to_dd(g["GPSLongitude"], g.get("GPSLongitudeRef", "E"))
                result["alt"] = round(_to_float(g.get("GPSAltitude", 0)), 1)
            elif tag == "DateTime":
                result["datetime"] = str(val)  # "2026:05:17 12:37:40"
            elif tag == "Make":
                result["make"] = str(val)
            elif tag == "Model":
                result["model"] = str(val)
    except Exception as e:
        print(f"  [WARN] EXIF読取失敗: {path.name} — {e}")
    return result

def parse_exif_datetime(dt_str: str) -> datetime | None:
    """EXIF の "YYYY:MM:DD HH:MM:SS" を JST aware datetime に変換。"""
    if not dt_str:
        return None
    try:
        dt = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
        return dt.replace(tzinfo=JST)
    except ValueError:
        return None

def dt_to_notion(dt: datetime) -> str:
    """datetime を Notion API 用 ISO8601 文字列に変換。"""
    return dt.isoformat()  # "2026-05-17T12:37:40+09:00"

# ============================================================
# 区切り写真の判定
# ============================================================

def _img_to_b64(path: Path, max_size: int = 800) -> str:
    """画像をリサイズして base64 文字列を返す。"""
    import io
    img = Image.open(path)
    # PNG等でRGBAモードの場合はJPEG保存できないためRGBに変換する
    if img.mode == "RGBA":
        img = img.convert("RGB")
    img.thumbnail((max_size, max_size))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=75)
    return base64.b64encode(buf.getvalue()).decode()

def _call_claude(prompt: str, b64_img: str) -> str:
    """Claude Vision API を呼び出してテキストを返す。"""
    headers = {
        "Content-Type":    "application/json",
        "x-api-key":       ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
    }
    payload = {
        "model": "claude-sonnet-4-6",
        "max_tokens": 512,
        "temperature": 0,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64_img}},
                {"type": "text",  "text": prompt},
            ],
        }],
    }
    resp = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=30)
    resp.raise_for_status()
    return resp.json()["content"][0]["text"].strip()

def ocr_form(path: Path) -> dict | None:
    """
    調査記入票をOCRして各項目を辞書で返す。
    記入票でない場合は None を返す。

    返却例:
    {
        "no": "1", "kouken": "河川", "L": "5.0", "H": "2.0",
        "S": "", "saigai_type": "", "surveyor": "東豊", "memo": ""
    }
    """
    b64 = _img_to_b64(path)
    prompt = (
        "この写真を見てください。\n\n"
        "【ステップ1】この写真に「災害調査の記入票・現地記録票・調査票」が写っていますか？\n\n"
        "以下のキーワードが【1つでも】読み取れれば記入票と判断してください。\n"
        "（斜め撮影・手持ち・部分的にしか写っていなくてもOKです）\n\n"
        "判断キーワード（どれか1つでも見えればYES）：\n"
        "  「災害調査」「現地記録票」「調査票」「NO」「No」「工種」\n"
        "  「L」「H」「S」「L延長」「H高さ」「斜長」\n"
        "  「調査者」「施工業者」「災害種類」「コメント」\n\n"
        "記入票と判断できれば「YES」、明らかに被災現場・風景・機材などの写真であれば「NO」と\n"
        "最初の行に答えてください。判断に迷う場合は「YES」としてください。\n\n"
        "【ステップ2】YESの場合のみ、以下のルールで読み取ってください。\n\n"
        "重要なルール：\n"
        "・この記入票は複数工区分の枠が縦に並んでいる場合があります\n"
        "・必ず【最初の工区枠（最上段のNO欄）だけ】を読んでください\n"
        "・2段目以降の工区枠は無視してください\n"
        "・手書きで値が記入されている欄だけ答えてください\n"
        "・空欄・未記入の欄は省略してください\n"
        "・斜めや手持ちで撮影されていても、読める文字を最大限読み取ってください\n"
        "・数値は小数点に注意して正確に読んでください（5.0は5.0、2.0は2.0）\n"
        "・数値は数字のみ（単位不要）\n\n"
        "NO=（最上段の番号のみ 例: 1）\n"
        "工種=（工種欄の手書き文字 例: 河川）\n"
        "L=（L欄の数値 例: 5.0）\n"
        "H=（H欄の数値 例: 2.0）\n"
        "S=（S欄の数値 例: 4.8）\n"
        "災害種類=（災害種類欄の内容）\n"
        "調査者=（調査者欄の名前 例: 東豊）\n"
        "コメント=（コメント欄の内容）\n"
    )
    try:
        text = _call_claude(prompt, b64)
        first_line = text.strip().split("\n")[0].strip().upper()
        if not first_line.startswith("YES"):
            return None

        def extract(key):
            m = re.search(rf"{key}\s*=\s*([^\n]*)", text)
            return m.group(1).strip() if m else ""

        def fix_num(v):
            """100m→10.0, 50m→5.0 のような明らかな誤読を補正しない（そのまま返す）。
            ただし末尾のmや単位を除去する。"""
            import re as _re
            v = _re.sub(r"[mMｍ㎡]$", "", v.strip())
            return v
        return {
            "no":           extract("NO"),
            "kouken":       extract("工種"),
            "L":            fix_num(extract("L")),
            "H":            fix_num(extract("H")),
            "S":            fix_num(extract("S")),
            "saigai_type":  extract("災害種類"),
            "surveyor":     extract("調査者"),
            "memo":         extract("コメント"),
        }
    except Exception as e:
        print(f"  [WARN] 記入票OCR失敗: {path.name} — {e}")
        return None

def is_divider(path: Path) -> tuple[bool, str, str, dict | None]:
    """
    区切り写真かどうかを判定する。
    ① 記入票OCR（最優先）
    ② No紙の簡易OCR
    ③ 手のひら検出
    returns: (is_div: bool, no_label: str, div_type: str, form: dict|None)
    form は記入票OCRの結果。2重呼び出しを防ぐため呼び出し元に返す。
    """
    b64 = _img_to_b64(path)

    # --- ① 記入票OCRで判定（最優先） ---
    form = ocr_form(path)
    if form is not None:
        no_label = f"No{form['no']}" if form["no"] else ""
        return True, no_label, "記入票", form

    # --- ② No紙の簡易OCR ---
    if DIVIDER_MODE in ("ocr", "both"):
        try:
            text = _call_claude(
                "この写真を見てください。"
                "災害調査の工区番号として「No1」「No2」などの番号が手書きされた紙・付箋・メモが写っていますか？\n"
                "条件：①手書き文字であること ②工区番号として意図された番号であること "
                "③看板・標識・印刷物の番号は対象外\n"
                "該当する場合は番号だけ答えてください（例：No3）。該当しない場合は「なし」とだけ答えてください。",
                b64,
            )
            m = re.search(r"[Nn][Oo]\.?\s*(\d+)", text)
            if m:
                return True, f"No{m.group(1)}", "No紙", None
        except Exception as e:
            print(f"  [WARN] OCR失敗: {path.name} — {e}")

    # --- ③ 手のひら検出 ---
    if DIVIDER_MODE in ("palm", "both"):
        try:
            text = _call_claude(
                "この写真に人の手のひらが大きく写っていますか？「はい」か「いいえ」だけで答えてください。",
                b64,
            )
            # 先頭が「はい」で始まる場合のみTrueとする（「はい、でも...」等の誤検知を防ぐ）
            if text.strip().startswith("はい"):
                return True, "", "手のひら", None
        except Exception as e:
            print(f"  [WARN] 手のひら判定失敗: {path.name} — {e}")

    return False, "", "", None

def ai_read_photo(path: Path) -> dict:
    """
    Claude API で写真内容を読み取り、推定工種・所見を返す。
    記入票の場合は ocr_form の結果をそのまま使うため、
    ここでは状況写真（記入票以外）の読取に特化。
    returns: {"ai_type": str, "ai_memo": str}
    """
    b64 = _img_to_b64(path)
    prompt = (
        "この写真は土木・道路・河川・法面などの被災調査写真です。"
        "以下の2点を日本語で簡潔に答えてください。\n"
        "① 推定される災害工種（道路土工・法面工・護岸工・排水工・舗装工・橋梁工・砂防工・その他 から1つ選択）\n"
        "② 写真から読み取れる被災状況の所見（2〜3文）\n"
        "形式: ①工種\n②所見"
    )
    try:
        text = _call_claude(prompt, b64)
        lines = text.strip().split("\n")
        ai_type = lines[0].replace("①", "").strip() if lines else "（判定中）"
        ai_memo = "\n".join(l.replace("②", "").strip() for l in lines[1:] if l.strip())
        return {"ai_type": ai_type, "ai_memo": ai_memo}
    except Exception as e:
        print(f"  [WARN] AI読取失敗: {path.name} — {e}")
        return {"ai_type": "（判定中）", "ai_memo": ""}

# ============================================================
# ZIP自動解凍
# ============================================================

def extract_zips_in_folder(folder: Path) -> int:
    """
    folder内のZIPファイルを解凍して同フォルダに展開し、
    解凍済みZIPを ../02_処理済/ へ移動する。
    サブフォルダ構造はフラット化して folder 直下に展開する。
    """
    import zipfile
    import shutil

    zip_files = [p for p in folder.iterdir() if p.suffix.lower() == ".zip"]
    if not zip_files:
        return 0

    done_dir = folder.parent / "02_処理済"
    done_dir.mkdir(exist_ok=True)

    total = 0
    for zip_path in zip_files:
        print(f"[INFO] ZIP解凍中: {zip_path.name}")
        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for info in zf.infolist():
                    # Windows製ZIPの日本語ファイル名（CP932）に対応
                    raw_name = info.filename
                    if not (info.flag_bits & 0x800):
                        try:
                            raw_name = info.filename.encode("cp437").decode("cp932")
                        except (UnicodeDecodeError, UnicodeEncodeError):
                            pass
                    # ディレクトリエントリをスキップ
                    if raw_name.endswith("/") or raw_name.endswith("\\"):
                        continue
                    # サブフォルダを除いたファイル名のみで展開（フラット化）
                    out_name = Path(raw_name).name
                    if not out_name:
                        continue
                    out_path = folder / out_name
                    out_path.write_bytes(zf.read(info))
                    print(f"  → {out_name}")
                    total += 1
            shutil.move(str(zip_path), str(done_dir / zip_path.name))
            print(f"[INFO] {zip_path.name} を 02_処理済/ へ移動しました。")
        except Exception as e:
            print(f"[WARN] ZIP解凍失敗: {zip_path.name} — {e}")

    if total:
        print(f"[INFO] ZIP解凍完了: 計 {total} ファイルを展開しました。")
    return total

# ============================================================
# フォルダスキャンと工区分割
# ============================================================

_COPY_PAT = re.compile(r'^(.+) \((\d+)\)(\.[^.]+)$')

def scan_folder(folder: Path) -> list[dict]:
    """フォルダ内の画像を撮影日時順にソートして返す。
    Box/Dropbox 同期で生成される "BASENAME (N).EXT" コピーは除外する。
    """
    all_files = sorted(
        [p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS],
        key=lambda p: p.name,
    )

    # コピーファイル除外
    all_names: set[str] = {p.name for p in all_files}
    seen_no_base: set[str] = set()   # 元ファイルが存在しない場合の重複防止
    files: list[Path] = []
    skipped: list[str] = []
    for p in all_files:
        m = _COPY_PAT.match(p.name)
        if m:
            base_name = m.group(1) + m.group(3)   # "BASENAME.EXT"
            if base_name in all_names:
                # 元ファイルが存在 → コピーをスキップ
                skipped.append(p.name)
                continue
            else:
                # 元ファイルが存在しない → 同一ベースの最初の1枚だけ残す
                if base_name in seen_no_base:
                    skipped.append(p.name)
                    continue
                seen_no_base.add(base_name)
        files.append(p)

    if skipped:
        print(f"[INFO] {len(skipped)} 枚のコピーファイルをスキップしました:")
        for name in skipped:
            print(f"       スキップ: {name}")
    print(f"[INFO] {len(files)} 枚の写真を検出: {folder}")
    return [extract_exif(p) for p in files]

def split_into_kouku(metas: list[dict], vendor: str) -> list[dict]:
    """
    メタデータリストを工区単位に分割する。
    区切り写真（No紙 or 手のひら）を先頭にして1工区を構成。
    """
    kouku_list = []
    current = None
    kouku_no = 0

    for i, meta in enumerate(metas):
        path = Path(meta["path"])
        print(f"  [{i+1}/{len(metas)}] {meta['file']} を判定中...", end=" ", flush=True)

        div, no_label, div_type, form = is_divider(path)

        if div:
            # 前の工区を確定
            if current:
                kouku_list.append(current)
            kouku_no += 1
            label = no_label if no_label else f"No{kouku_no}"
            print(f"→ 区切り検出（{div_type}）: {label}")

            # is_divider がすでにOCRを実行済みなので再呼び出し不要

            current = {
                "kouku_no":      kouku_no,
                "label":         label,
                "div_file":      meta["file"],
                "_div_path":     meta["path"],   # サムネイル生成用フルパス
                "div_type":      div_type,
                "lat":           meta["lat"],
                "lon":           meta["lon"],
                "alt":           meta["alt"],
                "datetime_raw":  meta["datetime"],
                "make":          meta["make"],
                "model":         meta["model"],
                "vendor":        vendor,
                "photos":        [],
                "ai_type":       form["kouken"] if form else "",
                "ai_memo":       "",
                # 記入票から読み取った値
                "form_no":       form["no"]          if form else "",
                "form_kouken":   form["kouken"]      if form else "",
                "form_L":        form["L"]           if form else "",
                "form_H":        form["H"]           if form else "",
                "form_S":        form["S"]           if form else "",
                "form_saigai":   form["saigai_type"] if form else "",
                "form_surveyor": form["surveyor"]    if form else "",
                "form_memo":     form["memo"]        if form else "",
            }

            # 記入票以外の場合はAI工種判定
            if div_type != "記入票":
                ai = ai_read_photo(path)
                current["ai_type"] = ai["ai_type"]
                current["ai_memo"] = ai["ai_memo"]
            else:
                # 記入票の内容をAI所見として整形
                if form is None:
                    current["ai_memo"] = "記入票OCR: 読取失敗"
                    current["_ocr_has_warning"] = True
                    print("     記入票OCR: 読取失敗（ocr_formがNoneを返しました）")
                else:
                    parts = []
                    if form.get("no"):       parts.append(f"NO={form['no']}")
                    if form.get("kouken"):   parts.append(f"工種={form['kouken']}")
                    if form.get("L"):        parts.append(f"L={form['L']}m")
                    if form.get("H"):        parts.append(f"H={form['H']}m")
                    if form.get("S"):        parts.append(f"S={form['S']}m")
                    if form.get("saigai"):   parts.append(f"災害種類={form['saigai_type']}")
                    if form.get("surveyor"): parts.append(f"調査者={form['surveyor']}")
                    if form.get("memo"):     parts.append(f"コメント={form['memo']}")
                    current["ai_memo"] = "記入票OCR: " + ", ".join(parts)
                    print(f"     記入票読取: {', '.join(parts)}")

                    # ── OCR読取りが怪しい場合の警告判定 ──────────────
                    warnings = []
                    no_val = form.get("no", "")
                    # ① No番号が空・記号混入・桁が多すぎる
                    if not no_val or not re.match(r"^\d{1,3}$", str(no_val).strip()):
                        warnings.append(f"No番号が不明瞭（読取値: '{no_val}'）")
                    # ② 工種が空、または項目名が値に混入（L= H= など）
                    kouken_val = form.get("kouken", "")
                    if not kouken_val or "=" in str(kouken_val) or "種類" in str(kouken_val):
                        warnings.append(f"工種が不明瞭（読取値: '{kouken_val}'）")
                    # ③ L/H/S に「=」や「m」以外の記号が混入
                    for key in ("L", "H", "S"):
                        val = str(form.get(key, ""))
                        if val and ("=" in val or "種類" in val):
                            warnings.append(f"{key}の値が不明瞭（読取値: '{val}'）")

                    if warnings:
                        print("     " + "⚠" * 3 + " OCR読取り注意 " + "⚠" * 3)
                        for w in warnings:
                            print(f"       - {w}")
                        print(f"       → 写真: {meta['file']}")
                        print(f"       → Notion登録後、画面で確認・修正してください")
                        current["_ocr_has_warning"] = True  # 要修正フラグ
        else:
            print("→ 通常写真")
            if current:
                current["photos"].append(meta["file"])

    if current:
        kouku_list.append(current)

    print(f"\n[INFO] {len(kouku_list)} 工区を検出しました。")
    return kouku_list

# ============================================================
# 配置図HTML生成
# ============================================================

def _make_thumb_b64(img_path: Path, width: int = 120, height: int = 90) -> str:
    """写真をサムネイルに縮小してbase64文字列を返す。失敗時は空文字。"""
    import io
    try:
        img = Image.open(img_path)
        img.thumbnail((width, height))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""

def generate_map_html(kouku_list: list[dict], output_dir: Path, vendor: str,
                      is_remap: bool = False) -> Path:
    """
    2列レイアウト（左：写真リスト、右：GSI地図）の配置図HTMLを生成する。
    写真サムネイルはbase64埋め込みのためインターネット不要・BOX/GDrive認証不要。
    """
    if not kouku_list:
        return None

    lats = [k["lat"] for k in kouku_list if k["lat"]]
    lons = [k["lon"] for k in kouku_list if k["lon"]]
    clat = (max(lats) + min(lats)) / 2 if lats else 33.32
    clon = (max(lons) + min(lons)) / 2 if lons else 130.91

    COLORS = ["#1D9E75","#D85A30","#BA7517","#534AB7","#185FA5","#993556",
              "#378ADD","#D4A017","#2E86AB","#A23B72"]

    # ── 各工区のサムネイル（一覧用）とモーダル用画像をbase64で取得 ──────────
    print("[INFO] サムネイル生成中...")
    for k in kouku_list:
        div_path = Path(k.get("_div_path", "")) if k.get("_div_path") else None
        if div_path and div_path.exists():
            k["_thumb_b64"] = _make_thumb_b64(div_path, 120, 90)
            k["_full_b64"]  = _make_thumb_b64(div_path, 640, 480)
        else:
            k["_thumb_b64"] = ""
            k["_full_b64"]  = ""

    # ── JS用DATAオブジェクト生成 ──────────────────────────────
    js_data_items = []
    for i, k in enumerate(kouku_list):
        c = COLORS[i % len(COLORS)]
        L_val = k.get("form_L") or ""
        H_val = k.get("form_H") or ""
        kouken = k.get("form_kouken") or k.get("ai_type") or "（未確定）"
        fname_js = k.get("div_file") or (k["label"] + ".jpg")
        cs = k.get("confirmation_status", "")
        item = (
            f'{{no:"{k["label"]}",'
            f'lat:{k["lat"] or clat},'
            f'lon:{k["lon"] or clon},'
            f'hasGps:{"true" if (k["lat"] and k["lon"]) else "false"},'
            f'kouken:"{kouken}",'
            f'color:"{c}",'
            f'L:"{L_val}",'
            f'H:"{H_val}",'
            f'gyosha:"{k["vendor"]}",'
            f'hatchu:"",'
            f'photos:{k["photo_count"]},'
            f'date:"{k["datetime_raw"] or ""}",'
            f'status:"写真収集済",'
            f'memo:"{k["ai_memo"][:60] if k["ai_memo"] else ""}",'
            f'notion_url:"{k.get("_notion_url","")}",'
            f'confirmation_status:"{cs}",'
            f'thumb:"{k["_thumb_b64"]}",'
            f'full:"{k["_full_b64"]}",'
            f'filename:"{fname_js}"}}'
        )
        js_data_items.append(item)
    js_data = "[\n" + ",\n".join(js_data_items) + "\n]"

    total_photos = sum(k["photo_count"] for k in kouku_list)
    remap_badge = ('<span style="background:#1D9E75;color:#fff;font-size:10px;'
                   'padding:2px 8px;border-radius:8px;margin-left:10px;">採用写真のみ</span>'
                   if is_remap else '')

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>配置図 — {vendor}</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Meiryo','Hiragino Sans',sans-serif;}}
body{{background:#f4f4f0;height:100vh;display:flex;flex-direction:column;overflow:hidden;}}
.hdr{{background:#185FA5;color:#fff;padding:8px 14px;display:flex;align-items:center;flex-shrink:0;}}
.hdr h1{{font-size:14px;font-weight:500;}}
.sum{{background:#fff;border-bottom:1px solid #ddd;padding:4px 14px;display:flex;gap:16px;flex-shrink:0;}}
.si{{display:flex;flex-direction:column;align-items:center;}}
.sn{{font-size:18px;font-weight:600;color:#185FA5;line-height:1;}}
.sl{{font-size:10px;color:#888;margin-top:1px;}}
.filters{{background:#fff;border-bottom:1px solid #ddd;padding:5px 14px;display:flex;gap:8px;align-items:center;flex-shrink:0;flex-wrap:wrap;}}
.filters label{{font-size:11px;color:#666;}}
.filters select{{font-size:11px;padding:2px 6px;border:1px solid #ddd;border-radius:6px;}}
.filters input{{font-size:11px;padding:2px 8px;border:1px solid #ddd;border-radius:6px;width:120px;}}
.fcnt{{font-size:11px;color:#888;margin-left:auto;}}
.main{{display:flex;flex:1;overflow:hidden;}}
.list{{width:230px;flex-shrink:0;overflow-y:auto;background:#f8f8f8;border-right:2px solid #ddd;}}
.list-item{{display:flex;gap:8px;padding:8px;border-bottom:1px solid #eee;cursor:pointer;transition:background 0.1s;align-items:flex-start;}}
.list-item:hover{{background:#EBF3FF;}}
.list-item.active{{background:#D6E8FA;border-left:3px solid #185FA5;}}
.list-item.hidden{{display:none;}}
.li-thumb{{width:72px;height:54px;object-fit:cover;border-radius:4px;flex-shrink:0;background:#ddd;cursor:zoom-in;}}
.li-nophoto{{width:72px;height:54px;border-radius:4px;flex-shrink:0;background:#e8e8e8;display:flex;align-items:center;justify-content:center;font-size:22px;}}
.li-info{{flex:1;min-width:0;}}
.li-no{{font-size:12px;font-weight:600;color:#222;display:flex;align-items:center;gap:4px;flex-wrap:wrap;}}
.li-badge{{font-size:9px;color:#fff;padding:1px 5px;border-radius:6px;}}
.li-sub{{font-size:10px;color:#666;margin-top:2px;line-height:1.5;}}
.li-lh{{font-weight:600;color:#C0392B;}}
.mapside{{flex:1;display:flex;flex-direction:column;overflow:hidden;}}
.maptabs{{background:#fff;border-bottom:1px solid #ddd;padding:4px 10px;display:flex;gap:6px;align-items:center;flex-shrink:0;}}
.tab{{padding:3px 12px;border-radius:12px;font-size:11px;cursor:pointer;border:1px solid #ddd;background:#f8f8f5;}}
.tab.active{{background:#185FA5;color:#fff;border-color:#185FA5;}}
.tab.fix-on{{background:#1D7A3A;color:#fff;border-color:#1D7A3A;}}
#fix-hint{{display:none;font-size:10px;color:#1D7A3A;margin-left:auto;}}
#map{{flex:1;width:100%;border:none;z-index:1;}}
.detail{{background:#fff;border-top:2px solid #185FA5;padding:6px 14px;flex-shrink:0;display:none;max-height:140px;overflow-y:auto;}}
.detail.show{{display:flex;gap:12px;align-items:flex-start;}}
.det-img{{width:80px;height:60px;object-fit:cover;border-radius:4px;flex-shrink:0;cursor:zoom-in;}}
.det-info{{flex:1;font-size:11px;}}
.det-title{{font-size:13px;font-weight:600;margin-bottom:4px;}}
.det-row{{display:flex;gap:8px;margin-bottom:2px;}}
.det-lb{{color:#888;min-width:50px;}}
.det-vr{{font-weight:600;color:#C0392B;}}
.det-links{{display:flex;gap:6px;margin-top:4px;flex-wrap:wrap;}}
.det-links a{{font-size:10px;color:#185FA5;text-decoration:none;background:#EBF3FF;padding:2px 6px;border-radius:8px;}}
.det-links a:hover{{background:#185FA5;color:#fff;}}
.det-close{{margin-left:auto;cursor:pointer;color:#aaa;font-size:16px;align-self:flex-start;}}
.img-modal{{display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,0.85);align-items:center;justify-content:center;flex-direction:column;}}
.img-modal.show{{display:flex;}}
.img-modal-img{{max-width:90vw;max-height:78vh;border-radius:6px;object-fit:contain;box-shadow:0 4px 24px rgba(0,0,0,0.6);}}
.img-modal-bar{{display:flex;gap:10px;margin-top:12px;align-items:center;}}
.img-modal-close{{color:#fff;font-size:30px;cursor:pointer;position:absolute;top:12px;right:18px;line-height:1;opacity:.8;user-select:none;}}
.img-modal-close:hover{{opacity:1;}}
.img-modal-dl{{background:#185FA5;color:#fff;padding:6px 18px;border-radius:16px;text-decoration:none;font-size:13px;font-weight:600;}}
.img-modal-dl:hover{{background:#0d4a8a;}}
.img-modal-cap{{color:#ccc;font-size:12px;}}
.hdr-toggle{{display:flex;gap:4px;margin-left:16px;}}
.hdr-btn{{background:transparent;border:1px solid rgba(255,255,255,0.5);color:#fff;padding:3px 10px;border-radius:12px;font-size:11px;cursor:pointer;font-family:inherit;transition:background 0.15s;}}
.hdr-btn.active{{background:#fff;color:#185FA5;border-color:#fff;font-weight:600;}}
.hdr-btn:hover:not(.active){{background:rgba(255,255,255,0.2);}}
/* Notion button in list items */
.li-notion{{display:inline-flex;align-items:center;font-size:9px;background:#000;color:#fff;padding:1px 5px;border-radius:4px;text-decoration:none;margin-left:4px;flex-shrink:0;vertical-align:middle;line-height:1.6;}}
.li-notion:hover{{background:#444;}}
/* Mobile bottom tab bar */
.mob-bar{{display:none;flex-shrink:0;background:#fff;border-top:2px solid #ddd;padding-bottom:env(safe-area-inset-bottom,0);}}
.mob-tab{{flex:1;text-align:center;padding:10px 4px 8px;font-size:12px;cursor:pointer;color:#666;border:none;background:transparent;font-family:inherit;line-height:1.3;}}
.mob-tab.active{{color:#185FA5;border-top:3px solid #185FA5;font-weight:700;}}
/* Notion floating button (mobile) */
.notion-fab{{display:none;position:fixed;bottom:70px;right:14px;z-index:600;background:#000;color:#fff;border-radius:50%;width:48px;height:48px;align-items:center;justify-content:center;font-size:20px;font-weight:700;text-decoration:none;box-shadow:0 3px 12px rgba(0,0,0,0.35);}}
.notion-fab.mob-show{{display:flex;}}
/* Tablet (768–899px): list narrower */
@media(max-width:899px)and(min-width:768px){{
  .list{{width:190px;}}
  .li-sub{{font-size:9px;}}
  .li-lh{{font-size:9px;}}
}}
/* Mobile (≤767px): stacked single-panel layout */
@media(max-width:767px){{
  body{{height:100dvh;}}
  .hdr{{padding:6px 10px;}}
  .hdr h1{{font-size:13px;}}
  .hdr-toggle{{margin-left:8px;}}
  .hdr-btn{{padding:2px 8px;font-size:10px;}}
  .sum{{padding:3px 10px;gap:10px;}}
  .sn{{font-size:16px;}}
  .sl{{font-size:9px;}}
  .filters{{padding:4px 10px;gap:5px;}}
  .filters input{{width:90px;}}
  .fcnt{{font-size:10px;}}
  .main{{flex-direction:column;}}
  .list{{width:100%;flex:1;border-right:none;border-bottom:2px solid #ddd;display:none;overflow-y:auto;}}
  .list.mob-show{{display:flex;flex-direction:column;}}
  .mapside{{display:none;flex:1;min-height:0;}}
  .mapside.mob-show{{display:flex;}}
  #map{{min-height:260px;}}
  .mob-bar{{display:flex;}}
  .detail{{max-height:190px;}}
  .det-lb{{min-width:42px;}}
  .det-links a{{font-size:11px;padding:3px 8px;}}
  .li-thumb{{width:60px;height:45px;}}
  .li-nophoto{{width:60px;height:45px;}}
}}
</style>
</head>
<body>
<div class="hdr">
  <h1>🗺 配置図 — {vendor}{remap_badge}</h1>
  <div class="hdr-toggle">
    <button class="hdr-btn active" id="btn-all" onclick="setSaiyoFilter('all')">全件表示</button>
    <button class="hdr-btn" id="btn-saiyo" onclick="setSaiyoFilter('saiyo')">採用のみ</button>
  </div>
  <span style="margin-left:auto;font-size:11px;opacity:.8;">写真をクリック→拡大 / 地図ピン→詳細</span>
</div>
<div class="sum">
  <div class="si"><span class="sn">{len(kouku_list)}</span><span class="sl">総箇所</span></div>
  <div class="si"><span class="sn">{total_photos}</span><span class="sl">写真枚数</span></div>
  <div class="si"><span class="sn" id="s-visible">{len(kouku_list)}</span><span class="sl">表示中</span></div>
</div>
<div class="filters">
  <label>工種</label>
  <select id="f-kouken" onchange="applyFilter()">
    <option value="">すべて</option>
    <option>護岸工</option><option>舗装工</option><option>道路土工</option>
    <option>法面工</option><option>橋梁工</option><option>排水工</option>
    <option>砂防工</option><option>（未確定）</option>
  </select>
  <label>検索</label>
  <input id="f-search" type="text" placeholder="No番号・工種・メモ" oninput="applyFilter()">
  <span class="fcnt" id="f-cnt">{len(kouku_list)}/{len(kouku_list)} 件</span>
</div>
<div class="main">
  <div class="list" id="list"></div>
  <div class="mapside">
    <div class="maptabs">
      <span class="tab active" onclick="setMap('std',this)">標準地図</span>
      <span class="tab" onclick="setMap('photo',this)">空中写真</span>
      <span class="tab" onclick="setMap('pale',this)">淡色地図</span>
      <span class="tab" id="fix-toggle" onclick="toggleFixMode()">✏️ 位置修正</span>
      <span id="fix-hint">位置修正モードON — ピンをドラッグして移動できます</span>
    </div>
    <div id="map" style="flex:1;width:100%;"></div>
    <div class="detail" id="detail">
      <img class="det-img" id="det-img" src="" alt="" title="クリックで拡大">
      <div class="det-info">
        <div class="det-title" id="det-title"></div>
        <div style="display:flex;gap:16px;flex-wrap:wrap;">
          <div>
            <div class="det-row"><span class="det-lb">業者名</span><span id="det-gyosha"></span></div>
            <div class="det-row"><span class="det-lb">撮影日</span><span id="det-date"></span></div>
          </div>
          <div>
            <div class="det-row"><span class="det-lb">L 延長</span><span class="det-vr" id="det-L"></span></div>
            <div class="det-row"><span class="det-lb">H 高さ</span><span class="det-vr" id="det-H"></span></div>
            <div class="det-row"><span class="det-lb">写真枚数</span><span id="det-photos"></span></div>
          </div>
          <div>
            <div class="det-row"><span class="det-lb">GPS</span><span id="det-gps" style="font-size:10px"></span></div>
            <div class="det-row"><span class="det-lb">配置</span><span id="det-placement" style="font-size:10px"></span></div>
            <div class="det-row"><span class="det-lb">メモ</span><span id="det-memo" style="font-size:10px"></span></div>
          </div>
        </div>
        <div class="det-links" id="det-links"></div>
      </div>
      <span class="det-close" onclick="closeDetail()">✕</span>
    </div>
  </div>
</div>
<div class="mob-bar">
  <button class="mob-tab active" id="mob-tab-list" onclick="mobShow('list')">📋<br>一覧</button>
  <button class="mob-tab" id="mob-tab-map" onclick="mobShow('map')">🗺<br>地図</button>
</div>
<a class="notion-fab" id="notion-fab" href="#" target="_blank"></a>
<div class="img-modal" id="img-modal" onclick="closeModal()">
  <span class="img-modal-close" onclick="closeModal()">✕</span>
  <img class="img-modal-img" id="modal-img" src="" alt="" onclick="event.stopPropagation()">
  <div class="img-modal-bar" onclick="event.stopPropagation()">
    <a class="img-modal-dl" id="modal-dl" href="#" download>ダウンロード</a>
    <span class="img-modal-cap" id="modal-cap"></span>
  </div>
</div>
<script>
var DATA = {js_data};
var clat={clat};var clon={clon};

// ── Leaflet 地図初期化（GSIタイル直接読込） ──────────────
var map = L.map('map').setView([clat, clon], 16);

var tileStd = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/std/{{z}}/{{x}}/{{y}}.png',
  {{attribution:'国土地理院', maxZoom:18}});
var tilePhoto = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/seamlessphoto/{{z}}/{{x}}/{{y}}.jpg',
  {{attribution:'国土地理院', maxZoom:18}});
var tilePale = L.tileLayer('https://cyberjapandata.gsi.go.jp/xyz/pale/{{z}}/{{x}}/{{y}}.png',
  {{attribution:'国土地理院', maxZoom:18}});

tileStd.addTo(map);  // 初期は標準地図

function setMap(type, el){{
  map.removeLayer(tileStd); map.removeLayer(tilePhoto); map.removeLayer(tilePale);
  if(type==='photo') tilePhoto.addTo(map);
  else if(type==='pale') tilePale.addTo(map);
  else tileStd.addTo(map);
  document.querySelectorAll('.tab').forEach(function(t){{t.classList.remove('active');}});
  el.classList.add('active');
}}

// ── ピン（番号付き円マーカー）を地図に配置 ──────────────
// markers[i] は DATA[i] に対応（GPS未設定の場合は undefined）
var markers = new Array(DATA.length);
// ── 引き出し線・元GPS位置ドット・位置修正モード ──────────
var leaderLines = new Array(DATA.length);
var anchorDots = new Array(DATA.length);
var origLatLngs = new Array(DATA.length);
var curLatLngs = new Array(DATA.length);
var manuallyMoved = new Array(DATA.length).fill(false);
var leaderLayer = L.layerGroup().addTo(map);
var LATLNG_EPS = 1e-8;

function pinIcon(d, i){{
  var borderStyle = d.hasGps ? '' : ';border-style:dashed';
  return L.divIcon({{
    className: 'pin-marker',
    html: '<div style="background:'+d.color+';width:26px;height:26px;border-radius:50%;border:2px solid #fff;display:flex;align-items:center;justify-content:center;color:#fff;font-size:12px;font-weight:700;box-shadow:0 2px 5px rgba(0,0,0,0.4)'+borderStyle+'">'+(i+1)+'</div>',
    iconSize: [26,26], iconAnchor: [13,13]
  }});
}}

function updateLeaderVisual(i){{
  var orig = origLatLngs[i], cur = curLatLngs[i];
  if(!orig || !cur) return;
  var differs = Math.abs(orig[0]-cur[0])>LATLNG_EPS || Math.abs(orig[1]-cur[1])>LATLNG_EPS;
  if(!differs){{
    if(leaderLines[i]){{ leaderLayer.removeLayer(leaderLines[i]); leaderLines[i]=null; }}
    if(anchorDots[i]){{ leaderLayer.removeLayer(anchorDots[i]); anchorDots[i]=null; }}
    return;
  }}
  if(!leaderLines[i]){{
    leaderLines[i] = L.polyline([orig, cur], {{color:'#185FA5', weight:2, opacity:.75, dashArray:'4,4'}}).addTo(leaderLayer);
  }} else {{
    leaderLines[i].setLatLngs([orig, cur]);
  }}
  if(!anchorDots[i]){{
    anchorDots[i] = L.circleMarker(orig, {{radius:5, color:'#fff', weight:2, fillColor:'#185FA5', fillOpacity:1}}).addTo(leaderLayer);
  }} else {{
    anchorDots[i].setLatLng(orig);
  }}
}}

function restoreOriginal(i){{
  var orig = origLatLngs[i];
  if(!orig || !markers[i]) return;
  curLatLngs[i] = orig.slice();
  manuallyMoved[i] = false;
  markers[i].setLatLng(orig);
  updateLeaderVisual(i);
  selectItem(i);
}}

var fixMode = false;
function toggleFixMode(){{
  fixMode = !fixMode;
  markers.forEach(function(m){{
    if(!m) return;
    if(fixMode) m.dragging.enable(); else m.dragging.disable();
  }});
  document.getElementById('fix-toggle').classList.toggle('fix-on', fixMode);
  document.getElementById('fix-hint').style.display = fixMode ? '' : 'none';
  document.getElementById('map').style.cursor = fixMode ? 'grab' : '';
}}

// ── 同一座標に重なるマーカーを自動で扇形に散らす（自動配置） ──────
// GPS未取得写真は地図中心にフォールバックするため、そのままでは
// 全ピンが1点に重なってしまう。同一座標(丸め誤差込み)のグループを検出し、
// 円周上に均等配置したうえで、引き出し線で元座標(アンカー)と結ぶ。
function computeAutoSpread(data){{
  var EPS = 5;              // 座標の丸め桁数（小数第5位=約1m単位でグルーピング）
  var RADIUS_DEG = 0.00006; // 扇形配置の半径（緯度方向、約6〜7m相当）
  var groups = {{}};
  data.forEach(function(d, i){{
    if(!d.lat || !d.lon) return;
    var key = d.lat.toFixed(EPS) + '_' + d.lon.toFixed(EPS);
    (groups[key] = groups[key] || []).push(i);
  }});
  var spread = {{}};
  Object.keys(groups).forEach(function(key){{
    var idxs = groups[key];
    if(idxs.length <= 1) return;  // 重複なしはそのまま
    var n = idxs.length;
    var baseLat = data[idxs[0]].lat, baseLon = data[idxs[0]].lon;
    var lonScale = Math.cos(baseLat * Math.PI / 180) || 1;
    idxs.forEach(function(i, j){{
      var angle = (2 * Math.PI * j) / n;
      spread[i] = [
        baseLat + RADIUS_DEG * Math.cos(angle),
        baseLon + (RADIUS_DEG * Math.sin(angle)) / lonScale
      ];
    }});
  }});
  return spread;
}}
var autoSpreadPositions = computeAutoSpread(DATA);

DATA.forEach(function(d, i){{
  if(!d.lat || !d.lon) return;
  origLatLngs[i] = [d.lat, d.lon];
  var initPos = autoSpreadPositions[i] || [d.lat, d.lon];
  curLatLngs[i] = initPos;
  var m = L.marker(initPos, {{icon:pinIcon(d,i), draggable:true}}).addTo(map);
  m.bindTooltip(d.no+' '+d.kouken, {{direction:'top'}});
  m.on('click', function(){{ selectItem(i); }});
  m.on('dragend', function(e){{
    var ll = e.target.getLatLng();
    curLatLngs[i] = [ll.lat, ll.lng];
    manuallyMoved[i] = true;
    updateLeaderVisual(i);
    if(document.getElementById('li-'+i).classList.contains('active')) selectItem(i);
  }});
  m.dragging.disable();
  markers[i] = m;
  if(autoSpreadPositions[i]) updateLeaderVisual(i);  // 自動配置分は最初から引き出し線を表示
}});

// 全ピンが収まるように表示範囲を自動フィット
var validMarkers = markers.filter(function(m){{ return !!m; }});
if(validMarkers.length>0){{
  var grp = L.featureGroup(validMarkers);
  map.fitBounds(grp.getBounds(), {{padding:[50,50], maxZoom:17}});
}}

// ── 拡大モーダル ────────────────────────────────────────
function openModal(i){{
  var d = DATA[i];
  var src = d.full || d.thumb;
  if(!src) return;
  document.getElementById('modal-img').src = src;
  document.getElementById('modal-dl').href = src;
  document.getElementById('modal-dl').setAttribute('download', d.filename || d.no+'.jpg');
  document.getElementById('modal-cap').textContent = d.no+' — '+d.kouken;
  document.getElementById('img-modal').classList.add('show');
}}
function closeModal(){{
  document.getElementById('img-modal').classList.remove('show');
}}
document.addEventListener('keydown', function(e){{ if(e.key==='Escape') closeModal(); }});

function buildList(){{
  var html='';
  DATA.forEach(function(d,i){{
    var lh='';
    if(d.L) lh+='L='+d.L+'m ';
    if(d.H) lh+='H='+d.H+'m';
    var thumbHtml=d.thumb
      ?'<img class="li-thumb" src="'+d.thumb+'" alt="'+d.no+'" title="クリックで拡大" onclick="event.stopPropagation();openModal('+i+')">'
      :'<div class="li-nophoto">📷</div>';
    html+='<div class="list-item" id="li-'+i+'" onclick="selectItem('+i+')">'
      +thumbHtml
      +'<div class="li-info">'
        +'<div class="li-no">'
          +'<span style="background:'+d.color+';width:18px;height:18px;border-radius:50%;display:inline-flex;align-items:center;justify-content:center;color:#fff;font-size:10px;font-weight:700;flex-shrink:0">'+(i+1)+'</span>'
          +' '+d.no
          +'<span class="li-badge" style="background:'+d.color+'">'+d.kouken+'</span>'
          +(d.notion_url?'<a class="li-notion" href="'+d.notion_url+'" target="_blank" onclick="event.stopPropagation()">N</a>':'')
        +'</div>'
        +'<div class="li-sub">'
          +(lh?'<span class="li-lh">'+lh.trim()+'</span><br>':'')
          +d.gyosha+'<br>写真'+d.photos+'枚'
        +'</div>'
      +'</div></div>';
  }});
  document.getElementById('list').innerHTML=html;
}}
function selectItem(i){{
  var d=DATA[i];
  var cur=curLatLngs[i]||[d.lat,d.lon];
  if(d.lat && d.lon){{
    map.flyTo(cur, 18, {{duration:0.6}});
    if(markers[i]) markers[i].openTooltip();
  }}
  document.querySelectorAll('.list-item').forEach(function(el){{el.classList.remove('active');}});
  document.getElementById('li-'+i).classList.add('active');
  var gmap='https://www.google.com/maps?q='+cur[0]+','+cur[1];
  var gsi='https://maps.gsi.go.jp/#18/'+cur[0]+'/'+cur[1]+'/';
  var detImg=document.getElementById('det-img');
  if(d.thumb){{
    detImg.src=d.thumb;
    detImg.style.display='';
    detImg.onclick=function(){{ openModal(i); }};
  }} else {{
    detImg.style.display='none';
    detImg.onclick=null;
  }}
  document.getElementById('det-title').textContent=d.no+' — '+d.kouken;
  document.getElementById('det-title').style.color=d.color;
  document.getElementById('det-gyosha').textContent=d.gyosha;
  document.getElementById('det-date').textContent=d.date;
  document.getElementById('det-L').textContent=d.L?d.L+' m':'―';
  document.getElementById('det-H').textContent=d.H?d.H+' m':'―';
  document.getElementById('det-photos').textContent=d.photos+' 枚';
  document.getElementById('det-gps').textContent=cur[0].toFixed(6)+', '+cur[1].toFixed(6);
  var placeEl=document.getElementById('det-placement');
  if(manuallyMoved[i]){{
    placeEl.innerHTML='<span style="color:#E67E22;font-weight:600;">'+(d.hasGps?'手動修正済':'手動配置')+'</span>'
      +(d.hasGps?' <a href="javascript:void(0)" onclick="restoreOriginal('+i+')">📍元のGPS位置に戻す</a>':'');
  }} else {{
    placeEl.innerHTML=d.hasGps?'<span style="color:#1D9E75;font-weight:600;">GPS自動配置</span>':'<span style="color:#999;">位置未設定（地図中心・要手動配置）</span>';
  }}
  document.getElementById('det-memo').textContent=d.memo;
  document.getElementById('det-links').innerHTML=
    '<a href="'+gmap+'" target="_blank">📍GoogleMap</a>'
    +'<a href="'+gsi+'" target="_blank">🗾国土地理院</a>'
    +(d.notion_url?'<a href="'+d.notion_url+'" target="_blank" style="background:#000;color:#fff;font-weight:600;">📋 Notion</a>':'');
  var fab=document.getElementById('notion-fab');
  if(fab){{fab.href=d.notion_url||'#';fab.textContent='N';fab.classList.toggle('mob-show',!!d.notion_url);}}
  document.getElementById('detail').classList.add('show');
}}
function closeDetail(){{
  document.getElementById('detail').classList.remove('show');
  document.querySelectorAll('.list-item').forEach(function(el){{el.classList.remove('active');}});
  var fab=document.getElementById('notion-fab');if(fab)fab.classList.remove('mob-show');
}}

var saiyoOnly = false;
function setSaiyoFilter(mode){{
  saiyoOnly = (mode === 'saiyo');
  document.getElementById('btn-all').classList.toggle('active', !saiyoOnly);
  document.getElementById('btn-saiyo').classList.toggle('active', saiyoOnly);
  applyFilter();
}}
function applyFilter(){{
  var fk=document.getElementById('f-kouken').value;
  var fq=document.getElementById('f-search').value.toLowerCase();
  var cnt=0;
  DATA.forEach(function(d,i){{
    var el=document.getElementById('li-'+i);
    var show=true;
    if(fk&&d.kouken!==fk) show=false;
    if(fq&&!(d.no.toLowerCase().includes(fq)||d.kouken.toLowerCase().includes(fq)||d.memo.toLowerCase().includes(fq))) show=false;
    if(saiyoOnly&&d.confirmation_status!=='採用') show=false;
    el.classList.toggle('hidden',!show);
    if(markers[i]){{
      if(show){{ if(!map.hasLayer(markers[i])) markers[i].addTo(map); }}
      else{{ if(map.hasLayer(markers[i])) map.removeLayer(markers[i]); }}
    }}
    if(leaderLines[i]){{
      if(show){{ if(!leaderLayer.hasLayer(leaderLines[i])) leaderLines[i].addTo(leaderLayer); }}
      else{{ leaderLayer.removeLayer(leaderLines[i]); }}
    }}
    if(anchorDots[i]){{
      if(show){{ if(!leaderLayer.hasLayer(anchorDots[i])) anchorDots[i].addTo(leaderLayer); }}
      else{{ leaderLayer.removeLayer(anchorDots[i]); }}
    }}
    if(show) cnt++;
  }});
  document.getElementById('f-cnt').textContent=cnt+'/'+DATA.length+' 件';
  document.getElementById('s-visible').textContent=cnt;
}}
function mobShow(mode){{
  var isList=(mode==='list');
  document.getElementById('list').classList.toggle('mob-show',isList);
  document.querySelector('.mapside').classList.toggle('mob-show',!isList);
  document.getElementById('mob-tab-list').classList.toggle('active',isList);
  document.getElementById('mob-tab-map').classList.toggle('active',!isList);
  if(!isList)setTimeout(function(){{map.invalidateSize();}},200);
}}
if(window.innerWidth<768){{document.getElementById('list').classList.add('mob-show');}}
buildList();
setTimeout(function(){{ map.invalidateSize(); }}, 300);
</script>
</body>
</html>"""

    fname = output_dir / f"配置図_{vendor}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
    fname.write_text(html, encoding="utf-8")
    print(f"[INFO] 配置図HTML生成: {fname}")
    return fname

# ============================================================
# 工種マッピング（記入票の手書き→Notionセレクト値）
# ============================================================

KOUKEN_MAP = {
    "道路": "道路土工", "道路土工": "道路土工",
    "法面": "法面工",   "法面工": "法面工",
    "護岸": "護岸工",   "護岸工": "護岸工",   "河川": "護岸工",
    "排水": "排水工",   "排水工": "排水工",
    "舗装": "舗装工",   "舗装工": "舗装工",
    "橋梁": "橋梁工",   "橋梁工": "橋梁工",
    "砂防": "砂防工",   "砂防工": "砂防工",
}

def _map_kouken(text: str) -> str:
    """手書き工種テキストをNotionのセレクト値にマッピング。"""
    for key, val in KOUKEN_MAP.items():
        if key in text:
            return val
    return "その他"

# ============================================================
# Notion 登録
# ============================================================

NOTION_HEADERS = {
    "Authorization":  f"Bearer {NOTION_TOKEN}",
    "Content-Type":   "application/json",
    "Notion-Version": "2022-06-28",
}

# 災害調査写真DB のID（config.iniから読み込み）
PHOTO_DS_ID = _cfg.get("NOTION", "photo_db_id", fallback="36f4880e-37de-405c-a719-8132bad1eb67")

def photo_exists_in_notion(filename: str) -> bool:
    """写真DBにファイル名が既に登録されているか確認する（重複チェック）。"""
    payload = {
        "filter": {
            "property": "ファイル名",
            "title": {"equals": filename},
        },
        "page_size": 1,
    }
    try:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{PHOTO_DS_ID.replace('-', '')}/query",
            headers=NOTION_HEADERS,
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            return len(resp.json().get("results", [])) > 0
        print(f"  [WARN] 重複チェックAPIエラー: {resp.status_code}")
    except Exception as e:
        print(f"  [WARN] 重複チェック失敗: {filename} — {e}")
    return False

def fetch_notion_saiyo_kouku(vendor: str) -> list[dict]:
    """
    Notion写真DBから「確認ステータス=採用」の写真を全件取得し、
    工区番号でグループ化して generate_map_html 用のリストを返す。
    --remap モード専用。
    """
    payload: dict = {
        "filter": {
            "and": [
                {"property": "業者名",        "rich_text": {"contains": vendor}},
                {"property": "確認ステータス", "select":    {"equals":  "採用"}},
            ]
        },
        "page_size": 100,
    }

    pages: list = []
    while True:
        resp = requests.post(
            f"https://api.notion.com/v1/databases/{PHOTO_DS_ID.replace('-', '')}/query",
            headers=NOTION_HEADERS,
            json=payload,
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[WARN] Notion写真DB取得失敗: {resp.status_code} {resp.text[:100]}")
            break
        data = resp.json()
        pages.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data["next_cursor"]

    if not pages:
        return []

    kouku_map: dict[str, dict] = {}
    for page in pages:
        p = page["properties"]

        def _txt(key: str) -> str:
            v = p.get(key, {})
            items = v.get("title") or v.get("rich_text") or []
            return items[0]["text"]["content"] if items else ""

        def _num(key: str):
            return p.get(key, {}).get("number")

        label = _txt("工区番号")
        if not label:
            continue

        lat = _num("緯度")
        lon = _num("経度")

        if label not in kouku_map:
            kouku_map[label] = {
                "label":               label,
                "kouku_no":            0,
                "lat":                 lat,
                "lon":                 lon,
                "alt":                 _num("標高(m)"),
                "datetime_raw":        "",
                "ai_type":             "",
                "ai_memo":             _txt("メモ（落石・被災原因等）"),
                "vendor":              vendor,
                "div_type":            "",
                "form_L":              "",
                "form_H":              "",
                "form_kouken":         "",
                "confirmation_status": "採用",
                "_notion_url":         page.get("url", ""),
                "_div_path":           "",
                "_thumb_b64":          "",
                "photos":              [],
                "photo_count":         0,
            }
        kouku_map[label]["photo_count"] += 1
        # GPS未設定工区に後続レコードのGPSを補完
        if kouku_map[label]["lat"] is None and lat is not None:
            kouku_map[label]["lat"] = lat
            kouku_map[label]["lon"] = lon

    result = sorted(kouku_map.values(), key=lambda k: k["label"])
    for i, k in enumerate(result):
        k["kouku_no"] = i + 1
    print(f"[INFO] Notion「採用」写真: {len(pages)}枚 / {len(result)}工区")
    return result

_gdrive_link_cache: dict[str, str] = {}

def get_gdrive_url(local_path: Path) -> str:
    """
    rclone link でGoogle Drive上のファイル共有URLを取得する。
    リモートパス: gdrive:災害調査_写真投入/{vendor}/{filename}
    取得失敗・タイムアウト時は空文字を返す。
    """
    cache_key = str(local_path)
    if cache_key in _gdrive_link_cache:
        return _gdrive_link_cache[cache_key]

    # local_path 例: G:\マイドライブ\災害調査_写真投入\A001_東豊開発コンサルタント\IMG.JPG
    # → vendor=A001_東豊開発コンサルタント, filename=IMG.JPG
    vendor   = local_path.parent.name
    filename = local_path.name
    remote   = f"gdrive:災害調査_写真投入/{vendor}/{filename}"

    url = ""
    for attempt in range(1, 4):
        try:
            result = subprocess.run(
                ["rclone", "link", remote],
                capture_output=True,
                text=True,
                timeout=30,
            )
            url = result.stdout.strip() if result.returncode == 0 else ""
        except Exception:
            url = ""

        if url:
            break
        if attempt < 3:
            print(f"      [GDrive] 取得失敗 ({attempt}/3)、3秒後にリトライ: {filename}")
            time.sleep(3)

    _gdrive_link_cache[cache_key] = url
    if url:
        print(f"      [GDrive] 共有リンク取得: {filename} → {url[:60]}...")
    else:
        print(f"      [GDrive] 共有リンク取得失敗 (3回試行): {filename}")
    return url


def register_photo_to_notion(
    meta: dict,
    kouku_label: str,
    seq: int,
    photo_type: str,
    memo: str,
    gdrive_url: str,
    vendor: str,
    dry_run: bool,
) -> str | None:
    """
    写真1枚分を Notion 写真DB に登録する。
    gdrive_url が空の場合は写真閲覧リンクをスキップ。
    """
    dt = parse_exif_datetime(meta["datetime"])
    dt_iso = dt_to_notion(dt) if dt else None

    view_url = gdrive_url
    maps_url = (
        f"https://www.google.com/maps?q={meta['lat']},{meta['lon']}"
        if meta["lat"] is not None else ""
    )

    props: dict = {
        "ファイル名":    meta["file"],
        "工区番号":      kouku_label,
        "撮影順序":      seq,
        "業者名":        vendor,
        "確認ステータス": "確認待ち",
        "メモ（落石・被災原因等）": memo,
    }
    if photo_type:
        props["写真種別"] = photo_type
    if meta["lat"] is not None:
        props["緯度"]    = meta["lat"]
        props["経度"]    = meta["lon"]
        props["標高(m)"] = meta["alt"]
    if maps_url:
        props["GoogleMapsリンク"] = maps_url
    if view_url:
        props["写真閲覧リンク"] = view_url
    if meta.get("model"):
        props["端末"] = f"{meta.get('make','')} {meta.get('model','')}".strip()
    if dt_iso:
        props["date:撮影日時:start"]       = dt_iso
        props["date:撮影日時:is_datetime"] = 1

    if dry_run:
        print(f"    [DRY-RUN] 写真登録スキップ: {meta['file']} ({photo_type})")
        return None

    # 重複チェック：同名ファイルが既に登録済みの場合はスキップ
    if photo_exists_in_notion(meta["file"]):
        print(f"    [SKIP] 既登録のためスキップ: {meta['file']}")
        return None

    payload = {
        "parent": {"database_id": PHOTO_DS_ID.replace("-", "")},
        "properties": _build_photo_properties(props),
    }
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json=payload,
        timeout=20,
    )
    if resp.status_code == 200:
        url = resp.json().get("url", "")
        print(f"    [写真DB] {meta['file']} ({photo_type}) → {url}")
        return url
    else:
        print(f"    [ERROR] 写真DB登録失敗: {resp.status_code} {resp.text[:100]}")
        return None

def _build_photo_properties(props: dict) -> dict:
    """写真DB用のNotionプロパティ形式に変換する。"""
    result = {}
    for key, val in props.items():
        if val is None or val == "":
            continue
        if key == "ファイル名":
            result[key] = {"title": [{"text": {"content": str(val)}}]}
        elif key in ("date:撮影日時:start",):
            result.setdefault("撮影日時", {"date": {}})
            result["撮影日時"]["date"]["start"] = val
        elif key == "date:撮影日時:is_datetime":
            pass
        elif key in ("緯度", "経度", "標高(m)", "撮影順序"):
            result[key] = {"number": float(val)}
        elif key in ("写真種別", "確認ステータス"):
            result[key] = {"select": {"name": str(val)}}
        elif key in ("GoogleMapsリンク", "写真閲覧リンク"):
            result[key] = {"url": str(val)}
        else:
            result[key] = {"rich_text": [{"text": {"content": str(val)[:2000]}}]}
    return result

def register_to_notion(kouku: dict, map_path: Path | None, dry_run: bool,
                       folder_url: str = "", is_gdrive: bool = False) -> str | None:
    """
    1工区分のデータを Notion に登録してページURLを返す。
    dry_run=True のときは登録せずデータをプリントするだけ。
    """
    dt = parse_exif_datetime(kouku["datetime_raw"])
    dt_iso = dt_to_notion(dt) if dt else None

    props = {
        "箇所名（No番号）": kouku["label"],
        "ステータス":       "写真収集済",
        "業者名":           kouku["vendor"],
        "写真枚数":         kouku["photo_count"],
        "区切り方法":       kouku["div_type"],
        "AI推定災害種類":   kouku["ai_type"],
        "AI所見・メモ":     kouku["ai_memo"],
    }
    # 記入票OCRで読み取った数値・工種を自動登録
    if kouku.get("form_L"):
        try: props["L 延長(m)"] = float(kouku["form_L"])
        except: pass
    if kouku.get("form_H"):
        try: props["H 高さ(m)"] = float(kouku["form_H"])
        except: pass
    if kouku.get("form_S"):
        try: props["斜長(m)"] = float(kouku["form_S"])
        except: pass
    if kouku.get("form_kouken"):
        props["確定工種"] = _map_kouken(kouku["form_kouken"])
    if kouku["lat"] is not None:
        props["緯度"]  = kouku["lat"]
        props["経度"]  = kouku["lon"]
        props["標高(m)"] = kouku["alt"]
        # GoogleMaps リンクを緯度経度から自動生成
        props["地図リンク（GoogleMaps）"] = (
            f"https://www.google.com/maps?q={kouku['lat']},{kouku['lon']}"
        )
    if dt_iso:
        props["date:撮影日時:start"]       = dt_iso
        props["date:撮影日時:is_datetime"] = 1
    if map_path:
        props["配置図リンク"] = map_path.name  # 実運用ではBOX共有URLなど

    # ── 写真フォルダリンク（Google Drive優先、なければBOX）──
    if folder_url:
        if is_gdrive:
            # GDriveの場合は専用プロパティに書く（カラム名は後述で追加）
            props["写真フォルダリンク（GDrive）"] = folder_url
        else:
            props["写真フォルダリンク（BOX）"] = folder_url

    # ── OCR関連プロパティ ──────────────────────────────────
    # 記入票OCRの生テキストを「OCR元テキスト」へ
    ocr_raw_parts = []
    if kouku.get("form_no"):       ocr_raw_parts.append(f"NO={kouku['form_no']}")
    if kouku.get("form_kouken"):   ocr_raw_parts.append(f"工種={kouku['form_kouken']}")
    if kouku.get("form_L"):        ocr_raw_parts.append(f"L={kouku['form_L']}")
    if kouku.get("form_H"):        ocr_raw_parts.append(f"H={kouku['form_H']}")
    if kouku.get("form_S"):        ocr_raw_parts.append(f"S={kouku['form_S']}")
    if kouku.get("form_saigai"):   ocr_raw_parts.append(f"災害種類={kouku['form_saigai']}")
    if kouku.get("form_surveyor"): ocr_raw_parts.append(f"調査者={kouku['form_surveyor']}")
    if kouku.get("form_memo"):     ocr_raw_parts.append(f"コメント={kouku['form_memo']}")

    if ocr_raw_parts:
        props["OCR元テキスト"] = " / ".join(ocr_raw_parts)

    # OCR確認状況：警告がある場合は「要修正」、記入票あり→「未確認」、それ以外は空欄
    if kouku.get("_ocr_has_warning"):
        props["OCR確認状況"] = "要修正"
    elif kouku.get("div_type") == "記入票":
        props["OCR確認状況"] = "未確認"

    if dry_run:
        print(f"\n[DRY-RUN] Notion登録スキップ: {json.dumps(props, ensure_ascii=False, indent=2)}")
        return None

    # Notion API ページ作成
    payload = {
        "parent": {"database_id": NOTION_DS_ID.replace("-", "")},
        "properties": _build_notion_properties(props),
    }
    resp = requests.post(
        "https://api.notion.com/v1/pages",
        headers=NOTION_HEADERS,
        json=payload,
        timeout=20,
    )
    if resp.status_code == 200:
        url = resp.json().get("url", "")
        print(f"  [OK] Notion登録完了: {kouku['label']} — {url}")
        return url
    else:
        print(f"  [ERROR] Notion登録失敗: {resp.status_code} {resp.text[:200]}")
        return None

def _build_notion_properties(props: dict) -> dict:
    """フラットな辞書を Notion API プロパティ形式に変換する。"""
    result = {}
    for key, val in props.items():
        if val is None or val == "":
            continue

        if key == "箇所名（No番号）":
            result[key] = {"title": [{"text": {"content": str(val)}}]}
        elif key in ("date:撮影日時:start",):
            result.setdefault("撮影日時", {"date": {}})
            result["撮影日時"]["date"]["start"] = val
        elif key == "date:撮影日時:is_datetime":
            pass  # Notion REST API では start に時刻があれば自動で datetime 扱い
        elif key in ("緯度", "経度", "標高(m)", "写真枚数",
                     "L 延長(m)", "H 高さ(m)", "斜長(m)", "面積(m²)"):
            result[key] = {"number": float(val)}
        elif key in ("ステータス", "区切り方法", "確定工種", "発注者（市町村）", "OCR確認状況"):
            result[key] = {"select": {"name": str(val)}}
        elif key in ("BOXリンク", "配置図リンク", "地図リンク（GoogleMaps）",
                     "写真フォルダリンク（BOX）", "写真フォルダリンク（GDrive）"):
            result[key] = {"url": str(val)}
        else:
            result[key] = {"rich_text": [{"text": {"content": str(val)[:2000]}}]}
    return result

# ============================================================
# メイン処理
# ============================================================

def upload_to_github(map_path: Path, vendor: str) -> str | None:
    """
    配置図HTMLをGitHubリポジトリにコピーし、一覧ページを再生成してpushする。
    成功すれば公開URLを返す。
    """
    import shutil
    import subprocess

    if not GITHUB_ENABLED:
        return None
    if not GITHUB_REPO_DIR or not Path(GITHUB_REPO_DIR).exists():
        print(f"[WARN] GitHubリポジトリフォルダが見つかりません: {GITHUB_REPO_DIR}")
        return None
    if not map_path or not map_path.exists():
        return None

    repo = Path(GITHUB_REPO_DIR)

    # 日付_業者名.html でコピー
    date_str = datetime.now().strftime("%Y%m%d_%H%M")
    safe_vendor = re.sub(r"[^\w\u3040-\u30ff\u4e00-\u9fff]", "", vendor)  # ファイル名に使えない文字を除去
    dest_name = f"{date_str}_{safe_vendor}.html"
    dest_path = repo / dest_name

    try:
        shutil.copy(map_path, dest_path)
        print(f"[INFO] GitHubフォルダへコピー: {dest_name}")
    except Exception as e:
        print(f"[WARN] コピー失敗: {e}")
        return None

    # 固定URL: 業者コードプレフィックス.html としても保存（例: A001.html）
    _vm = re.match(r'^([A-Za-z0-9]+)', vendor)
    vendor_code = _vm.group(1) if _vm else re.sub(r'[^\w]', '', vendor)[:8]
    if vendor_code:
        fixed_dest = repo / f"{vendor_code}.html"
        try:
            shutil.copy(map_path, fixed_dest)
            print(f"[INFO] 固定URLファイルを保存: {vendor_code}.html")
        except Exception as e:
            print(f"[WARN] 固定URLコピー失敗: {e}")

    # 一覧ページ(index.html)を再生成
    _generate_index_page(repo)

    # git add / commit / push
    try:
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True,
                       capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", f"配置図追加 {dest_name}"],
                       cwd=repo, check=True, capture_output=True, text=True)
        result = subprocess.run(["git", "push"], cwd=repo,
                                capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print(f"[WARN] git push 失敗: {result.stderr}")
            return None
        print("[INFO] GitHubへアップロード完了")
    except subprocess.TimeoutExpired:
        print("[WARN] git push タイムアウト")
        return None
    except subprocess.CalledProcessError as e:
        # commit する変更が無い場合などもここに来る
        print(f"[WARN] git 操作: {e.stderr if e.stderr else e}")
        return None
    except Exception as e:
        print(f"[WARN] git 操作失敗: {e}")
        return None

    # 公開URL
    if GITHUB_PAGES_URL:
        base = GITHUB_PAGES_URL.rstrip("/")
        if vendor_code:
            print(f"[INFO] 固定公開URL: {base}/{vendor_code}.html")
        return f"{base}/{dest_name}"
    return None


def _generate_index_page(repo: Path):
    """リポジトリ内の配置図HTML一覧ページ(index.html)を生成する。"""
    # 固定URLファイル（A001.html など）と履歴ファイル（YYYYMMDD_*.html）を分類
    all_html = [f for f in repo.glob("*.html") if f.name != "index.html"]
    fixed_files = sorted([f for f in all_html if not re.match(r'^\d{8}_', f.name)], key=lambda f: f.name)
    hist_files  = sorted([f for f in all_html if     re.match(r'^\d{8}_', f.name)], key=lambda f: f.name, reverse=True)
    html_files  = hist_files  # for the count

    fixed_rows = "".join(
        f'<a class="item item-fixed" href="{f.name}">'
        f'<div class="item-main">'
        f'<span class="item-vendor">{f.stem}</span>'
        f'<span class="item-date">最新版（固定URL）</span>'
        f'</div><span class="item-arrow">▶</span></a>'
        for f in fixed_files
    )

    rows = ""
    for f in hist_files:
        name = f.stem  # 拡張子なし
        # 日付_業者名 を分解
        parts = name.split("_", 2)
        if len(parts) >= 3:
            date_disp = f"{parts[0][:4]}/{parts[0][4:6]}/{parts[0][6:8]}"
            time_disp = f"{parts[1][:2]}:{parts[1][2:]}" if len(parts[1]) == 4 else parts[1]
            vendor_disp = parts[2]
        else:
            date_disp = ""
            time_disp = ""
            vendor_disp = name
        rows += f"""
    <a class="item" href="{f.name}">
      <div class="item-main">
        <span class="item-vendor">{vendor_disp}</span>
        <span class="item-date">{date_disp} {time_disp}</span>
      </div>
      <span class="item-arrow">▶</span>
    </a>"""

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>災害調査 配置図一覧</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0;font-family:'Meiryo','Hiragino Sans',sans-serif;}}
body{{background:#f4f4f0;padding:0;}}
.hdr{{background:#185FA5;color:#fff;padding:14px 18px;}}
.hdr h1{{font-size:17px;font-weight:600;}}
.hdr p{{font-size:12px;opacity:.85;margin-top:3px;}}
.wrap{{max-width:720px;margin:0 auto;padding:16px;}}
.note{{background:#fff;border:1px solid #ddd;border-radius:8px;padding:12px 14px;font-size:12px;color:#666;margin-bottom:14px;line-height:1.6;}}
.item{{display:flex;align-items:center;background:#fff;border:1px solid #ddd;border-radius:8px;padding:14px 16px;margin-bottom:8px;text-decoration:none;color:#222;transition:box-shadow 0.15s;}}
.item:hover{{box-shadow:0 4px 12px rgba(0,0,0,0.12);border-color:#185FA5;}}
.item-main{{flex:1;display:flex;flex-direction:column;gap:3px;}}
.item-vendor{{font-size:15px;font-weight:600;color:#185FA5;}}
.item-date{{font-size:12px;color:#888;}}
.item-arrow{{color:#185FA5;font-size:13px;}}
.item-fixed{{border-color:#185FA5;background:#EBF3FF;}}
.section-title{{font-size:13px;font-weight:600;color:#185FA5;padding:10px 4px 4px;border-bottom:1px solid #ddd;margin-bottom:8px;}}
.empty{{text-align:center;color:#999;padding:40px;}}
</style>
</head>
<body>
<div class="hdr">
  <h1>🗺 災害調査 配置図一覧</h1>
  <p>東豊開発コンサルタント　／　見たい配置図を選んでください</p>
</div>
<div class="wrap">
  <div class="note">
    各配置図は撮影日・業者ごとに保存されています。新しいものが上に表示されます。<br>
    配置図を開くと、写真リストと地図・各箇所の詳細が確認できます。
  </div>
  {('<div class="section-title">📌 最新版（固定URL）</div>' + fixed_rows) if fixed_rows else ''}
  {('<div class="section-title">📁 履歴</div>' if fixed_rows and rows else '') + (rows if rows else ('<div class="empty">配置図がまだありません</div>' if not fixed_rows else ''))}
</div>
</body>
</html>"""

    index_path = repo / "index.html"
    index_path.write_text(html, encoding="utf-8")
    print(f"[INFO] 一覧ページ生成: {index_path.name}（{len(html_files)}件）")


def main():
    parser = argparse.ArgumentParser(description="災害調査写真 → Notion 自動登録")
    parser.add_argument("--folder",  default=r"G:\マイドライブ\災害調査_写真投入", help="写真フォルダのパス（省略時: G:\\マイドライブ\\災害調査_写真投入）")
    parser.add_argument("--vendor",  required=True, help="業者名")
    parser.add_argument("--dry-run", action="store_true", help="Notionへの書き込みをスキップしてデータを確認する")
    parser.add_argument("--remap",   action="store_true",
                        help="Notionの「採用」写真から配置図HTMLを再生成する（写真スキャン・登録はスキップ）")
    parser.add_argument("--box-url",       default="", help="BOX写真フォルダの共有URL（省略可・後方互換用）")
    parser.add_argument("--gdrive-folder", default="", help="Google Drive写真フォルダの共有URL（省略可）")
    args = parser.parse_args()

    # --remap モード：Notionから「採用」写真を取得して配置図を再生成
    if args.remap:
        map_dir = Path(MAP_OUTPUT_DIR) if MAP_OUTPUT_DIR else Path(args.folder or ".")
        print("=" * 60)
        print(f"  [REMAP] 採用写真のみで配置図を再生成")
        print(f"  業者名 : {args.vendor}")
        print("=" * 60)
        saiyo_list = fetch_notion_saiyo_kouku(args.vendor)
        if not saiyo_list:
            print("[WARN] 「採用」に設定された写真がありません。Notionで確認ステータスを更新してください。")
            sys.exit(0)
        map_path = generate_map_html(saiyo_list, map_dir, args.vendor, is_remap=True)
        if GITHUB_ENABLED:
            upload_to_github(map_path, args.vendor)
        print(f"\n  採用写真のみの配置図: {map_path}")
        print("=" * 60)
        sys.exit(0)

    folder = Path(args.folder)
    if not folder.exists():
        print(f"[ERROR] フォルダが見つかりません: {folder}")
        sys.exit(1)

    map_dir = Path(MAP_OUTPUT_DIR) if MAP_OUTPUT_DIR else folder

    print("=" * 60)
    print(f"  disaster_survey.py — 災害調査写真管理システム")
    print(f"  フォルダ : {folder}")
    print(f"  業者名   : {args.vendor}")
    print(f"  DRY-RUN  : {args.dry_run}")
    print("=" * 60)

    # ① ZIPファイルの自動解凍（01_写真投入フォルダ内のZIPを展開 → 02_処理済へ移動）
    extract_zips_in_folder(folder)

    # ② メタデータ取得（全写真）
    metas = scan_folder(folder)
    if not metas:
        print("[ERROR] 対象画像がありません。")
        sys.exit(1)

    # ③ 工区分割（区切り写真判定 + AI読取）
    kouku_list = split_into_kouku(metas, args.vendor)

    # 写真フォルダリンク（Google Drive優先、なければBOX）をkoukuに追加
    folder_url = args.gdrive_folder or args.box_url
    for k in kouku_list:
        k["folder_url"]    = folder_url
        k["is_gdrive"]     = bool(args.gdrive_folder)

    # 写真枚数を確定
    for k in kouku_list:
        k["photo_count"] = len(k["photos"])

    # ④ 配置図生成
    map_path = generate_map_html(kouku_list, map_dir, args.vendor)

    # ⑤ Notion 登録（工区DB + 写真DB）
    print("\n[INFO] Notion DB に登録中...")
    for k in kouku_list:
        # 工区DBに登録してNotionページURLを保存
        notion_url = register_to_notion(k, map_path, dry_run=args.dry_run, folder_url=k.get("folder_url",""), is_gdrive=k.get("is_gdrive", False))
        k["_notion_url"] = notion_url or ""

        # 写真DBに登録（区切り写真 + 状況写真 すべて）
        seq = 1

        # フォルダから全メタを再取得して順番通りに登録
        all_files_in_kouku = [k["div_file"]] + k["photos"]
        all_metas_ordered = [m for f in all_files_in_kouku
                             for m in metas if m["file"] == f]

        for meta in all_metas_ordered:
            is_div = meta["file"] == k["div_file"]
            photo_type = "記入票" if is_div and k["div_type"] == "記入票" else \
                         "その他" if is_div else "状況写真"
            memo = k["ai_memo"] if is_div else ""

            gdrive_url = get_gdrive_url(Path(meta["path"]))

            register_photo_to_notion(
                meta        = meta,
                kouku_label = k["label"],
                seq         = seq,
                photo_type  = photo_type,
                memo        = memo,
                gdrive_url  = gdrive_url,
                vendor      = k["vendor"],
                dry_run     = args.dry_run,
            )
            seq += 1

    # ⑥ Notion URL を埋め込んで配置図を再生成
    print("\n[INFO] 配置図HTML（NotionURL付き）を再生成中...")
    map_path = generate_map_html(kouku_list, map_dir, args.vendor)

    # ⑦ GitHub Pages へ自動アップロード（dry-runでなく、有効時のみ）
    github_url = None
    if not args.dry_run and GITHUB_ENABLED:
        print("\n[INFO] GitHub Pages へアップロード中...")
        github_url = upload_to_github(map_path, args.vendor)

    # ⑧ サマリー出力
    print("\n" + "=" * 60)
    print(f"  完了: {len(kouku_list)} 工区を処理しました。")
    for k in kouku_list:
        gps = f"{k['lat']}, {k['lon']}" if k["lat"] else "GPS なし"
        print(f"  [{k['label']}] {k['div_type']} / {k['photo_count']}枚 / {k['ai_type']} / {gps}")
    if map_path:
        print(f"\n  配置図（ローカル）: {map_path}")
    if github_url:
        print(f"  配置図（公開URL）: {github_url}")
        print(f"  一覧ページ: {GITHUB_PAGES_URL}")
    print("=" * 60)


if __name__ == "__main__":
    main()
