#!/usr/bin/env python3
"""
Bol-Ke-Loan  -  "Jo Bola vs Jo Dikhaya"
Voice-first loan interview in any Indian language + document cross-check, built on Sarvam AI.

  Saaras (speech-to-text, auto language detect)  ->  Sarvam-105B (interview brain + underwriting)
  Bulbul v3 (voice of the loan officer)          ->  Sarvam Vision (reads passbook / salary slip)

Run:   pip3 install -r requirements.txt   (only needed for Sarvam Vision)
       python3 server.py                  -> open http://localhost:8000
Key:   put SARVAM_API_KEY=... in .env (never commit it)
"""
import base64
import io
import json
import math
import os
import re
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(HERE, "static")


def load_env():
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
API_KEY = os.environ.get("SARVAM_API_KEY", "")
MOCK = os.environ.get("SARVAM_MOCK", "") == "1"
PORT = int(os.environ.get("PORT", "8000"))
BASE = "https://api.sarvam.ai"
CHAT_MODELS = [os.environ.get("SARVAM_CHAT_MODEL", "sarvam-105b"), "sarvam-m"]
STT_MODELS = [os.environ.get("SARVAM_STT_MODEL", "saaras:v4"), "saaras:v3", "saarika:v2.5"]
TTS_SPEAKER = os.environ.get("SARVAM_TTS_SPEAKER", "priya")
TTS_LANGS = {"hi-IN", "bn-IN", "ta-IN", "te-IN", "kn-IN", "ml-IN", "mr-IN", "gu-IN", "pa-IN", "od-IN", "en-IN"}
ANNUAL_RATE = 0.22  # typical micro-enterprise loan rate used for EMI math
MAX_FOIR = 0.50


class SarvamError(Exception):
    pass


# ----------------------------------------------------------------------------- HTTP helpers
def _headers(extra=None):
    h = {"api-subscription-key": API_KEY}
    if extra:
        h.update(extra)
    return h


def http_json(path, payload, timeout=120):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode("utf-8"),
        headers=_headers({"Content-Type": "application/json"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SarvamError(f"{path} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    except urllib.error.URLError as e:
        raise SarvamError(f"{path} network error: {e.reason}")


def http_multipart(path, fields, files, timeout=120):
    boundary = "----bkl" + uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        body.write(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                   f'Content-Type: {ctype}\r\n\r\n'.encode())
        body.write(data)
        body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())
    req = urllib.request.Request(
        BASE + path, data=body.getvalue(),
        headers=_headers({"Content-Type": f"multipart/form-data; boundary={boundary}"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SarvamError(f"{path} HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:400]}")
    except urllib.error.URLError as e:
        raise SarvamError(f"{path} network error: {e.reason}")


# ----------------------------------------------------------------------------- Sarvam wrappers
def stt(audio_bytes, lang_hint=None):
    """Saaras speech-to-text with automatic language detection."""
    if MOCK:
        return {"transcript": "मेरा नाम रमेश पाटिल है, मेरी किराना की दुकान है", "language_code": "hi-IN", "model": "mock"}
    last = None
    for model in STT_MODELS:
        fields = {"model": model, "language_code": lang_hint or "unknown"}
        if model.startswith("saaras"):
            fields["mode"] = "transcribe"
        try:
            r = http_multipart("/speech-to-text", fields, {"file": ("speech.wav", audio_bytes, "audio/wav")})
            return {"transcript": r.get("transcript", ""),
                    "language_code": r.get("language_code") or lang_hint or "hi-IN", "model": model}
        except SarvamError as e:
            last = e
            print("[stt] fallback:", e, file=sys.stderr)
    raise last


def chat(messages, json_mode=True, max_tokens=2000, temperature=0.3):
    """Sarvam-105B chat (falls back to sarvam-m). Returns (content, model)."""
    if MOCK:
        return mock_chat(messages), "mock"
    last = None
    for model in CHAT_MODELS:
        variants = []
        base = {"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if model != "sarvam-m":
            base["reasoning_effort"] = None  # off = faster replies for a live voice call
        if json_mode:
            variants.append(dict(base, response_format={"type": "json_object"}))
        variants.append(base)
        for payload in variants:
            try:
                r = http_json("/v1/chat/completions", payload, timeout=150)
                msg = r["choices"][0]["message"]
                content = msg.get("content") or ""
                if content.strip():
                    return content, model
                last = SarvamError(f"{model}: empty content (finish_reason={r['choices'][0].get('finish_reason')})")
            except SarvamError as e:
                last = e
                print(f"[chat] {model} failed:", e, file=sys.stderr)
    raise last


def parse_json(text):
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except Exception:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            return json.loads(text[s:e + 1])
        raise


def tts(text, lang):
    """Bulbul v3 text-to-speech. Returns list of base64 WAV chunks."""
    if MOCK:
        return [silent_wav_b64()]
    if lang not in TTS_LANGS:
        lang = "hi-IN"
    text = (text or "").strip()[:2400]
    if not text:
        return []
    attempts = [
        {"text": text, "target_language_code": lang, "model": "bulbul:v3", "speaker": TTS_SPEAKER},
        {"text": text, "language_code": lang, "model": "bulbul:v3", "speaker": TTS_SPEAKER},
        {"text": text, "target_language_code": lang, "model": "bulbul:v3"},
        {"inputs": [text[:1400]], "target_language_code": lang, "model": "bulbul:v2", "speaker": "anushka"},
    ]
    last = None
    for p in attempts:
        try:
            r = http_json("/text-to-speech", p, timeout=90)
            audios = r.get("audios") or ([r["audio"]] if r.get("audio") else [])
            if audios:
                return audios
        except SarvamError as e:
            last = e
            print("[tts] fallback:", e, file=sys.stderr)
    print("[tts] giving up:", last, file=sys.stderr)
    return []


def vision_read(img_bytes, fname, mime, lang):
    """Sarvam Vision (Document AI digitise) -> markdown text of the document."""
    if MOCK:
        return MOCK_PASSBOOK_MD
    try:
        from sarvamai import SarvamAI
    except ImportError:
        raise SarvamError("Sarvam Vision needs the SDK: run  pip3 install -U sarvamai")
    client = SarvamAI(api_subscription_key=API_KEY)
    if not hasattr(client, "doc_ai"):
        raise SarvamError("Your sarvamai SDK is old: run  pip3 install -U sarvamai")
    job = client.doc_ai.digitise(file=[(fname, io.BytesIO(img_bytes), mime)], language=lang, output_format="md")
    terminal = {"completed", "partially_completed", "failed", "rejected"}
    deadline = time.time() + 180
    status = ""
    while True:
        st = client.doc_ai.get_status(job_id=job.job_id)
        status = str(getattr(st.status, "value", st.status)).lower().split(".")[-1]
        if status in terminal:
            break
        if time.time() > deadline:
            raise SarvamError("Sarvam Vision job timed out")
        time.sleep(2)
    if status not in ("completed", "partially_completed"):
        raise SarvamError(f"Sarvam Vision job {status}")
    dl = client.doc_ai.get_download_url(job_id=job.job_id)
    data = urllib.request.urlopen(dl.url, timeout=90).read()
    try:
        z = zipfile.ZipFile(io.BytesIO(data))
        md = [z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.lower().endswith(".md")]
        if not md:
            md = [z.read(n).decode("utf-8", "replace") for n in z.namelist() if n.lower().endswith(".json")]
        return "\n\n".join(md)
    except zipfile.BadZipFile:
        return data.decode("utf-8", "replace")


# ----------------------------------------------------------------------------- Loan logic
FIELDS = {
    "full_name": "string",
    "age": "integer",
    "location": "string (village/town, district, state)",
    "occupation": "string (e.g. kirana shop owner, dairy farmer, tailor)",
    "years_in_business": "number",
    "monthly_income": "integer INR per month (net business/salary income)",
    "monthly_expenses": "integer INR per month (household + business)",
    "dependents": "integer",
    "existing_loans": "string (lender + purpose) or 'none'",
    "existing_emi": "integer INR per month total EMI (0 if none)",
    "loan_amount": "integer INR requested",
    "loan_purpose": "string",
    "tenure_months": "integer",
    "collateral": "string or 'none'",
}
REQUIRED = ["full_name", "occupation", "monthly_income", "monthly_expenses", "existing_emi", "loan_amount", "loan_purpose"]

INTERVIEW_SYSTEM = """You are "Sakhi", a warm, patient loan officer at a micro-finance lender in India, interviewing a small-business borrower over a VOICE call.

Rules:
- Speak ONLY in the borrower's language (code: {lang}). Use simple, everyday spoken words in native script. If the borrower mixes English words (Hinglish etc.), mirror that naturally.
- Keep every reply SHORT: at most 2 sentences, under 200 characters. It will be spoken aloud.
- Ask ONE question at a time. Briefly acknowledge the previous answer first (e.g. "Achha, 25 hazaar.").
- Collect these fields (store values in ENGLISH, numbers as plain integers in INR):
{fields}
- Skip anything already in the current form. If an answer is vague ("thoda bahut"), ask once more with an example, then accept a best estimate.
- Convert spoken numbers: "pacchees hazaar" = 25000, "dedh lakh" = 150000, "2 lakh" = 200000.
- When all of {required} are known (or after about 10 questions), thank them warmly, tell them the next step is to show a photo of their bank passbook / statement / salary slip, and set "done": true. Never promise approval.

Current form (JSON): {form}

Reply with ONLY a JSON object, no other text:
{{"reply": "<what you say next, in the borrower's language>", "reply_en": "<English translation of reply>", "user_en": "<English translation of the borrower's last message>", "lang": "<BCP-47 code of the borrower's language, e.g. hi-IN, mr-IN, ta-IN>", "form": {{<only the fields you learned or corrected in this turn>}}, "done": false}}"""

GREETINGS = {
    "hi-IN": "नमस्ते! मैं सखी हूँ, आपकी लोन सहायक। आप अपनी भाषा में बात कीजिए। पहले बताइए, आपका नाम क्या है?",
    "mr-IN": "नमस्कार! मी सखी, तुमची कर्ज सहाय्यक. तुम्ही तुमच्या भाषेत बोला. आधी सांगा, तुमचं नाव काय?",
    "ta-IN": "வணக்கம்! நான் சகி, உங்கள் கடன் உதவியாளர். உங்கள் மொழியிலேயே பேசுங்கள். முதலில், உங்கள் பெயர் என்ன?",
    "te-IN": "నమస్కారం! నేను సఖి, మీ లోన్ సహాయకురాలిని. మీ భాషలోనే మాట్లాడండి. ముందుగా, మీ పేరు ఏమిటి?",
    "kn-IN": "ನಮಸ್ಕಾರ! ನಾನು ಸಖಿ, ನಿಮ್ಮ ಸಾಲ ಸಹಾಯಕಿ. ನಿಮ್ಮ ಭಾಷೆಯಲ್ಲೇ ಮಾತನಾಡಿ. ಮೊದಲು, ನಿಮ್ಮ ಹೆಸರೇನು?",
    "bn-IN": "নমস্কার! আমি সখী, আপনার লোন সহায়ক। আপনার নিজের ভাষায় কথা বলুন। প্রথমে বলুন, আপনার নাম কী?",
    "gu-IN": "નમસ્તે! હું સખી, તમારી લોન સહાયક. તમારી ભાષામાં વાત કરો. પહેલા કહો, તમારું નામ શું છે?",
    "ml-IN": "നമസ്കാരം! ഞാൻ സഖി, നിങ്ങളുടെ ലോൺ സഹായി. നിങ്ങളുടെ ഭാഷയിൽ സംസാരിക്കൂ. ആദ്യം, നിങ്ങളുടെ പേര് എന്താണ്?",
    "pa-IN": "ਸਤ ਸ੍ਰੀ ਅਕਾਲ! ਮੈਂ ਸਖੀ ਹਾਂ, ਤੁਹਾਡੀ ਲੋਨ ਸਹਾਇਕ। ਆਪਣੀ ਭਾਸ਼ਾ ਵਿੱਚ ਗੱਲ ਕਰੋ। ਪਹਿਲਾਂ ਦੱਸੋ, ਤੁਹਾਡਾ ਨਾਮ ਕੀ ਹੈ?",
    "od-IN": "ନମସ୍କାର! ମୁଁ ସଖୀ, ଆପଣଙ୍କ ଋଣ ସହାୟିକା। ନିଜ ଭାଷାରେ କଥା ହୁଅନ୍ତୁ। ପ୍ରଥମେ କୁହନ୍ତୁ, ଆପଣଙ୍କ ନାମ କଣ?",
    "en-IN": "Hello! I'm Sakhi, your loan assistant. You can speak in any Indian language. First, what is your name?",
}


def to_num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).lower().replace(",", "").replace("₹", "").replace("rs.", "").replace("rs", "").strip()
    m = re.search(r"(\d+(\.\d+)?)", s)
    if not m:
        return None
    n = float(m.group(1))
    if "lakh" in s or "lac" in s:
        n *= 100000
    elif "crore" in s:
        n *= 10000000
    elif re.search(r"\d\s*k\b", s) or "thousand" in s or "hazar" in s:
        n *= 1000
    return n


def emi(principal, months, annual=ANNUAL_RATE):
    r = annual / 12
    months = max(int(months or 24), 1)
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def principal_for_emi(e, months, annual=ANNUAL_RATE):
    r = annual / 12
    months = max(int(months or 24), 1)
    return e * ((1 + r) ** months - 1) / (r * (1 + r) ** months)


def compute_numbers(form, facts):
    said_income = to_num(form.get("monthly_income"))
    said_emi = to_num(form.get("existing_emi")) or 0
    doc_income = to_num((facts or {}).get("avg_monthly_credits")) or to_num((facts or {}).get("salary_or_regular_income"))
    doc_emi = to_num((facts or {}).get("emi_or_loan_debits_monthly")) or 0
    tenure = int(to_num(form.get("tenure_months")) or 24)
    ask = to_num(form.get("loan_amount")) or 0
    income = min(x for x in [said_income, doc_income] if x) if (said_income or doc_income) else 0
    exist = max(said_emi, doc_emi)
    new_emi = emi(ask, tenure) if ask else 0
    foir = (exist + new_emi) / income if income else None
    room = max(MAX_FOIR * income - exist, 0) if income else 0
    eligible = principal_for_emi(room, tenure) if room else 0
    return {
        "said_income": said_income, "doc_income": doc_income, "assessed_income": income,
        "said_emi": said_emi, "doc_emi": doc_emi, "assessed_existing_emi": exist,
        "requested_amount": ask, "tenure_months": tenure, "interest_rate_pct": ANNUAL_RATE * 100,
        "new_emi": round(new_emi), "foir_pct": round(foir * 100, 1) if foir is not None else None,
        "max_eligible_amount": int(round(eligible / 1000.0) * 1000),
    }


MEMO_SYSTEM = """You are a senior credit underwriter at an Indian micro-lender (like AbleCredit's AI underwriting desk).
You get (1) what the borrower SAID in a voice interview, (2) facts extracted from their DOCUMENT by Sarvam Vision (may be null), and (3) computed affordability numbers (EMI at {rate}% p.a., FOIR cap {foir}%).

Do a "Jo Bola vs Jo Dikhaya" (said vs shown) cross-check. Flag every material mismatch: income, existing loans/EMIs undisclosed, name mismatch, bounced cheques/penalties, low balance, irregular credits. Be fair: small differences (<15%) are OK. Explain like a human underwriter, citing evidence.
Decide: "APPROVE", "APPROVE_REDUCED" (approve a lower amount = max_eligible_amount rounded), or "REFER" (needs field verification). Never reject a borrower for language/literacy.

Borrower language: {lang}.
Reply with ONLY JSON:
{{"decision": "APPROVE|APPROVE_REDUCED|REFER", "recommended_amount": <int INR>, "risk_score": <0-100, higher = riskier>, "headline": "<one-line English verdict>",
"mismatches": [{{"field": "<e.g. Monthly income>", "said": "<what they said>", "shown": "<what document shows>", "severity": "high|medium|low", "note": "<why it matters>"}}],
"strengths": ["<short>"], "concerns": ["<short>"], "conditions": ["<e.g. co-applicant, field visit>"],
"summary_en": "<4-6 sentence underwriting note for the credit committee>",
"borrower_message": "<3-4 short spoken sentences IN THE BORROWER'S LANGUAGE ({lang}), kind and clear: what we understood, what was found, the offer / next step. No jargon like FOIR.>"}}"""

DOC_SYSTEM = """You extract lending facts from a document that was OCR'd by Sarvam Vision (bank passbook, bank statement, salary slip, or ID).
Return ONLY JSON with these keys (null if not present; numbers as plain integers INR):
{"doc_type": "", "account_holder_name": "", "bank_or_employer": "", "period": "", "avg_monthly_credits": null, "salary_or_regular_income": null,
"emi_or_loan_debits_monthly": null, "emi_details": "", "closing_balance": null, "bounced_or_penalty_charges": "", "other_observations": ""}
For passbooks/statements: avg_monthly_credits = total credits / number of months covered. Look for EMI/loan/NACH/ECS/ACH debits and list them in emi_details."""


# ----------------------------------------------------------------------------- API handlers
def api_turn(body):
    lang = body.get("lang") or "hi-IN"
    form = body.get("form") or {}
    history = body.get("history") or []
    text = (body.get("text") or "").strip()
    fields_txt = "\n".join(f"  - {k}: {v}" for k, v in FIELDS.items())
    sys_msg = INTERVIEW_SYSTEM.format(lang=lang, fields=fields_txt, required=", ".join(REQUIRED),
                                      form=json.dumps(form, ensure_ascii=False))
    msgs = [{"role": "system", "content": sys_msg}, {"role": "user", "content": "(call connected)"}]
    for h in history[-16:]:
        role = "assistant" if h.get("role") == "assistant" else "user"
        if msgs[-1]["role"] == role:
            msgs[-1]["content"] += "\n" + h.get("content", "")
        else:
            msgs.append({"role": role, "content": h.get("content", "")})
    if msgs[-1]["role"] == "user":
        msgs[-1]["content"] += "\n" + text
    else:
        msgs.append({"role": "user", "content": text})
    content, model = chat(msgs, json_mode=True, max_tokens=1200)
    try:
        out = parse_json(content)
    except Exception:
        out = {"reply": content.strip()[:300], "form": {}, "done": False}
    out.setdefault("form", {})
    out["lang"] = out.get("lang") if out.get("lang") in TTS_LANGS else lang
    merged = dict(form)
    merged.update({k: v for k, v in (out.get("form") or {}).items() if v not in (None, "", "unknown")})
    out["form_full"] = merged
    out["missing"] = [k for k in REQUIRED if merged.get(k) in (None, "")]
    out["audio"] = tts(out.get("reply", ""), out["lang"])
    out["model"] = model
    return out


def api_stt(body):
    audio = base64.b64decode(body["audio_b64"])
    hint = body.get("lang") if body.get("lang") not in (None, "", "auto") else None
    return stt(audio, hint)


def api_greet(body):
    lang = body.get("lang") or "hi-IN"
    if lang not in GREETINGS:
        lang = "hi-IN"
    text = GREETINGS[lang]
    return {"reply": text, "reply_en": GREETINGS["en-IN"], "lang": lang, "audio": tts(text, lang)}


def api_doc(body):
    img = base64.b64decode(body["image_b64"])
    md = vision_read(img, body.get("filename") or "document.png", body.get("mime") or "image/png",
                     body.get("doc_lang") or "en-IN")
    content, model = chat([{"role": "system", "content": DOC_SYSTEM},
                           {"role": "user", "content": "OCR text from Sarvam Vision:\n\n" + md[:12000]}],
                          json_mode=True, max_tokens=1500, temperature=0.1)
    facts = parse_json(content)
    return {"markdown": md[:6000], "facts": facts, "model": model}


def api_memo(body):
    form = body.get("form") or {}
    facts = body.get("facts")
    lang = body.get("lang") or "hi-IN"
    nums = compute_numbers(form, facts)
    user = json.dumps({"borrower_said": form, "document_facts": facts, "computed": nums}, ensure_ascii=False, indent=1)
    content, model = chat([{"role": "system", "content": MEMO_SYSTEM.format(lang=lang, rate=int(ANNUAL_RATE * 100),
                                                                           foir=int(MAX_FOIR * 100))},
                           {"role": "user", "content": user}], json_mode=True, max_tokens=2500, temperature=0.2)
    memo = parse_json(content)
    memo["numbers"] = nums
    memo["audio"] = tts(memo.get("borrower_message", ""), lang if lang in TTS_LANGS else "hi-IN")
    memo["model"] = model
    return memo


ROUTES = {"/api/turn": api_turn, "/api/stt": api_stt, "/api/greet": api_greet, "/api/doc": api_doc, "/api/memo": api_memo}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[http] " + (fmt % args) + "\n")

    def _send(self, code, data, ctype="application/json"):
        if not isinstance(data, (bytes, bytearray)):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/health":
            return self._send(200, {"ok": True, "key_loaded": bool(API_KEY), "mock": MOCK,
                                    "chat_model": CHAT_MODELS[0], "stt_model": STT_MODELS[0]})
        if path == "/":
            path = "/index.html"
        fp = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not fp.startswith(STATIC) or not os.path.isfile(fp):
            return self._send(404, {"error": "not found"})
        ctype = {".html": "text/html; charset=utf-8", ".png": "image/png", ".jpg": "image/jpeg",
                 ".js": "text/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(
            os.path.splitext(fp)[1], "application/octet-stream")
        with open(fp, "rb") as f:
            self._send(200, f.read(), ctype)

    def do_POST(self):
        fn = ROUTES.get(self.path.split("?")[0])
        if not fn:
            return self._send(404, {"error": "not found"})
        if not API_KEY and not MOCK:
            return self._send(500, {"error": "SARVAM_API_KEY missing. Put it in the .env file next to server.py"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            t = time.time()
            out = fn(body)
            out["ms"] = int((time.time() - t) * 1000)
            self._send(200, out)
        except SarvamError as e:
            self._send(502, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})


# ----------------------------------------------------------------------------- mock mode (offline backup demo)
MOCK_PASSBOOK_MD = """SAHYADRI GRAMIN SAHAKARI BANK (SAMPLE)  Account holder: RAMESH S PATIL  Branch: Sinnar, Nashik
| Date | Particulars | Debit | Credit | Balance |
| 05-07 | UPI-COLLECTIONS | | 7,400 | 9,850 |
| 10-07 | NACH-SHRI FINANCE EMI | 4,500 | | 5,350 |
| 18-07 | CASH DEP | | 8,200 | 13,550 |
| 28-07 | UPI-COLLECTIONS | | 6,900 | 20,450 |
| 10-08 | NACH-SHRI FINANCE EMI | 4,500 | | 14,100 |
| 14-08 | CASH DEP | | 13,600 | 27,700 |
| 30-08 | UPI-COLLECTIONS | | 8,700 | 31,900 |
| 10-09 | NACH-SHRI FINANCE EMI | 4,500 | | 23,600 |
| 22-09 | CASH DEP | | 21,300 | 44,900 |"""

_mock_i = {"n": 0}


def mock_chat(messages):
    sysm = messages[0]["content"]
    if sysm.startswith("You extract"):
        return json.dumps({"doc_type": "Bank passbook", "account_holder_name": "Ramesh S Patil",
                           "bank_or_employer": "Sahyadri Gramin Sahakari Bank", "period": "Jul-Sep",
                           "avg_monthly_credits": 22033, "salary_or_regular_income": None,
                           "emi_or_loan_debits_monthly": 4500, "emi_details": "NACH Shri Finance EMI 4,500 monthly",
                           "closing_balance": 34600, "bounced_or_penalty_charges": "none",
                           "other_observations": "Mostly UPI and cash deposits"})
    if sysm.startswith("You are a senior"):
        return json.dumps({"decision": "APPROVE_REDUCED", "recommended_amount": 100000, "risk_score": 58,
                           "headline": "Genuine business, but income overstated and an EMI was not disclosed.",
                           "mismatches": [
                               {"field": "Monthly income", "said": "₹40,000", "shown": "~₹22,000 avg credits", "severity": "high", "note": "Said income is ~80% above banked credits."},
                               {"field": "Existing loans", "said": "None", "shown": "₹4,500/month NACH EMI to Shri Finance", "severity": "high", "note": "Undisclosed EMI changes repayment capacity."}],
                           "strengths": ["Stable 8-year kirana business", "Regular UPI collections"],
                           "concerns": ["Overstated income", "Undisclosed loan"], "conditions": ["Field visit to shop"],
                           "summary_en": "Mock memo.", "borrower_message": "रमेश जी, धन्यवाद। आपकी पासबुक में हर महीने लगभग 22 हज़ार रुपये आते हैं और 4500 की एक EMI चल रही है। इसलिए हम आपको 1 लाख रुपये का लोन दे सकते हैं।"})
    _mock_i["n"] += 1
    n = _mock_i["n"]
    steps = [({"full_name": "Ramesh Patil", "location": "Sinnar, Nashik, Maharashtra"}, "अच्छा रमेश जी। आप क्या काम करते हैं?"),
             ({"occupation": "Kirana shop owner", "years_in_business": 8}, "बढ़िया। हर महीने कितनी कमाई होती है?"),
             ({"monthly_income": 40000}, "ठीक है। घर और दुकान का खर्च कितना है?"),
             ({"monthly_expenses": 18000, "dependents": 4}, "कोई और लोन या EMI चल रही है?"),
             ({"existing_loans": "none", "existing_emi": 0}, "कितना लोन चाहिए और किस काम के लिए?"),
             ({"loan_amount": 200000, "loan_purpose": "Buy a refrigerator and stock for the shop", "tenure_months": 24, "collateral": "none"},
              "धन्यवाद रमेश जी! अब अपनी बैंक पासबुक की फोटो दिखाइए।")]
    f, r = steps[min(n - 1, len(steps) - 1)]
    return json.dumps({"reply": r, "reply_en": "(mock)", "user_en": "(mock)", "lang": "hi-IN", "form": f, "done": n >= len(steps)})


def silent_wav_b64():
    sr, n = 16000, 1600
    data = b"\x00\x00" * n
    hdr = b"RIFF" + (36 + len(data)).to_bytes(4, "little") + b"WAVEfmt " + (16).to_bytes(4, "little") + \
        (1).to_bytes(2, "little") + (1).to_bytes(2, "little") + sr.to_bytes(4, "little") + (sr * 2).to_bytes(4, "little") + \
        (2).to_bytes(2, "little") + (16).to_bytes(2, "little") + b"data" + len(data).to_bytes(4, "little")
    return base64.b64encode(hdr + data).decode()


if __name__ == "__main__":
    print("=" * 60)
    print(" Bol-Ke-Loan  |  http://localhost:%d" % PORT)
    print(" Sarvam key loaded:", bool(API_KEY), "| mock mode:", MOCK)
    print(" Chat:", CHAT_MODELS[0], "| STT:", STT_MODELS[0], "| TTS: bulbul:v3 (%s)" % TTS_SPEAKER)
    print("=" * 60)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
