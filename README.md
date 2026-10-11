# Bol-Ke-Loan — "Jo Bola vs Jo Dikhaya"

**A voice-first loan officer for Bharat that checks what the borrower *said* against what their documents *show*.**

190M+ Indian small businesses and farmers are credit-starved. Loan forms are in English and assume literacy, so the borrower depends on an agent who fills in numbers *for* them. Underwriters then get inflated incomes and hidden EMIs, and they respond by rejecting everyone.

Bol-Ke-Loan fixes both ends of the problem:
1. **The borrower just talks** to "Sakhi", an AI loan officer, in Hindi, Marathi, Tamil, Hinglish or any Indian language. The loan application fills itself in English, live.
2. **The borrower shows one photo** of a passbook, statement or salary slip, and Sarvam Vision reads it.
3. **"Jo Bola vs Jo Dikhaya"**: the AI underwriter cross-checks the spoken claims against the document. It flags overstated income and undisclosed EMIs, computes affordability (the share of income going to EMIs, called FOIR), and gives an explainable decision: approve, approve a reduced amount, or refer for a field check.
4. **The borrower hears the decision** in their own language, kindly and without jargon.

## Sarvam AI stack
| Step | Sarvam model |
|---|---|
| Borrower's voice to text, auto language detection, code-mixed speech | **Saaras v4** (`/speech-to-text`) |
| Interview brain, form extraction, underwriting memo | **Sarvam-105B** (`/v1/chat/completions`, JSON mode) |
| Sakhi's voice and the spoken decision | **Bulbul v3** (`/text-to-speech`) |
| Reading the passbook / statement photo | **Sarvam Vision** (Document AI `digitise`) |

Affordability math (EMI, FOIR, maximum eligible amount) is computed in plain code, not by the LLM, so the numbers are deterministic and auditable.

## Run it
You need **Python 3.9+** and a **Sarvam API key** from https://dashboard.sarvam.ai. The only dependency is the `sarvamai` SDK; everything else is the Python standard library.

```bash
git clone https://github.com/prajyotdev530/sarvam-loan-agent.git
cd sarvam-loan-agent

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env               # Windows: copy .env.example .env
# open .env and set SARVAM_API_KEY=<your key>

python server.py
```
Open **http://localhost:8000** in Chrome. Chrome allows the microphone on `localhost`.

**One command on macOS / Linux:** `./start.sh` creates the virtualenv, installs the requirements, creates `.env` from the example if it's missing, starts the server and opens the browser.

The key is read only from `.env`, which is git-ignored, and is never hard-coded. Optional settings (port, models, voice) are listed in `.env.example`.

**No key? Backup demo:** `SARVAM_MOCK=1 python server.py` runs the whole flow offline with canned responses. It needs no key or network, which is useful when the Wi-Fi dies on stage.

## 90-second demo script
1. Press **Start call**. Sakhi greets the borrower in Hindi.
2. Press **Demo borrower**, or speak yourself in Marathi or Tamil. Watch the application fill in live.
   - In the demo, Ramesh says he earns ₹40,000 a month and has no loans.
3. **Use sample passbook**, then **Read with Sarvam Vision**.
   - The passbook shows about ₹22,000 a month in credits and a ₹4,500 NACH EMI.
4. **Generate underwriting memo**.
   - Two HIGH mismatches are flagged, the share of income going to EMIs is above 50%, and the result is **Approve reduced amount (about ₹1L instead of ₹2L)**.
   - The borrower hears the decision in Hindi.

## Running-loan mode: "My running loan"
After disbursal, the same borrower can call Sakhi back and ask, in any language:
- **Next installment**: amount, due date, and the principal and interest split
- **Total interest**: over the whole loan, how much is paid so far, and how much is left
- **Remaining EMIs**: how many are paid, how many are left, and when the last one falls due

Switch to **💳 My running loan** in the phone panel. Then tap a quick question, speak, or press **Demo Qs**. The lender side shows the account summary and the full repayment schedule, and highlights the figures Sakhi just quoted.

For now this uses a **sample loan** (`SAMPLE_LOAN` in `server.py`): Ramesh's ₹1,00,000 reduced approval at 22% over 24 months, first EMI on 5 May 2026. Every EMI whose due date has passed counts as paid. The schedule is reducing-balance amortisation, computed in code (`loan_status()`). The model only reads those numbers back in the borrower's language and never calculates figures itself. To use real data, replace `SAMPLE_LOAN`/`loan_status()` with a lookup in your loan book.

| Endpoint | What it does |
|---|---|
| `POST /api/loan` | Loan summary and full schedule (works without a Sarvam key) |
| `POST /api/loan_ask` | `{text, history, lang}` → Sakhi's spoken answer, plus `topic` and the refreshed loan |

## Why it matters
- **Inclusion:** a borrower who cannot read the form can still apply in their own language.
- **Trust:** every decision is explainable, with the evidence shown beside each claim.
- **Lender ROI:** catches undisclosed loans and over-statement *before* disbursal, and approves a right-sized amount instead of rejecting outright.
