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

## Run it (Mac)
```bash
cd ~/Documents/sarvam
./start.sh            # installs the sarvamai SDK (for Vision) and starts the app
# or:  pip3 install -r requirements.txt && python3 server.py
```
Open **http://localhost:8000** in Chrome. The key is read from `.env` (`SARVAM_API_KEY=...`), which is git-ignored and never hard-coded.

**Backup demo:** `SARVAM_MOCK=1 python3 server.py` runs the whole flow offline with canned responses, for when the Wi-Fi dies on stage.

## 90-second demo script
1. Press **Start call**. Sakhi greets the borrower in Hindi.
2. Press **Demo borrower**, or speak yourself in Marathi or Tamil. Watch the application fill in live.
   - In the demo, Ramesh says he earns ₹40,000 a month and has no loans.
3. **Use sample passbook**, then **Read with Sarvam Vision**.
   - The passbook shows about ₹22,000 a month in credits and a ₹4,500 NACH EMI.
4. **Generate underwriting memo**.
   - Two HIGH mismatches are flagged, the share of income going to EMIs is above 50%, and the result is **Approve reduced amount (about ₹1L instead of ₹2L)**.
   - The borrower hears the decision in Hindi.

## Why it matters
- **Inclusion:** a borrower who cannot read the form can still apply in their own language.
- **Trust:** every decision is explainable, with the evidence shown beside each claim.
- **Lender ROI:** catches undisclosed loans and over-statement *before* disbursal, and approves a right-sized amount instead of rejecting outright.
