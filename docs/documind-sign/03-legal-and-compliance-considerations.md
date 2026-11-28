# DocuMind Sign — 03. Legal and compliance considerations

> **This is an engineering requirements analysis, not legal advice.** It lists what the product
> must do or record so that a qualified lawyer can assess it. **DocuMind Sign makes no claim of
> legal validity, enforceability or compliance in any jurisdiction until such a review has taken
> place.** It doesn't claim eIDAS-qualified status, QES, or that signatures are "legally
> binding everywhere".

## 1. Status of the sources

Every law below is marked **TO BE VERIFIED AGAINST OFFICIAL TEXT**.

- **Checked during M0 (2026-11-28):** a web search found only secondary sources: vendor pages,
  law-firm notes and academic theses.
- **Not obtained:** the official texts (Federal Negarit Gazette, EUR-Lex Official Journal, U.S.
  Code).
- **The secondary sources disagree on details.** For example, they give different dates for
  Proclamation 1205/2020: publication on 6 April 2020, or entry into force on 30 June 2020. Only
  the official text settles this.

| Jurisdiction | Instrument | Status | What the secondary sources suggest (unverified) |
|---|---|---|---|
| Ethiopia | Electronic Signature Proclamation No. 1072/2018 | TO BE VERIFIED | Adopted December 2018. One source says it governs electronic signatures outside the scope of 1205/2020. A law firm describes signature categories: simple electronic signatures, digital signatures and "reliable" electronic signatures |
| Ethiopia | Electronic Transactions Proclamation No. 1205/2020 | TO BE VERIFIED | Published in the Federal Negarit Gazette in 2020. Covers electronic transactions and electronic contracts. Excludes some matters, such as personal status (marriage and divorce). Related material was published by the Ethiopian Communications Authority |
| Ethiopia | Personal data protection law | TO BE VERIFIED (identify the instrument) | A personal-data protection framework is reported; the instrument number and scope must be confirmed |
| EU | eIDAS, Regulation (EU) No 910/2014, as amended by Regulation (EU) 2024/1183 (and its corrigendum) | TO BE VERIFIED | The consolidated text defines electronic, advanced and qualified electronic signatures. A qualified signature needs a qualified signature creation device and a qualified certificate. The 2024 amendment mainly adds the European Digital Identity Wallet framework |
| EU | GDPR, Regulation (EU) 2016/679 | TO BE VERIFIED for the use case | Governs signer data (names, emails, IP addresses) |
| US | ESIGN Act, 15 U.S.C. § 7001 et seq. | TO BE VERIFIED | Electronic signatures and records may not be denied effect solely for being electronic; consumer-consent disclosure requirements apply in some cases |
| US | UETA (state enactments) | TO BE VERIFIED per state | Adopted by most states; at least one state uses its own statute |

**Secondary sources consulted (orientation only):**
- [Six key points on the electronic signature law in Ethiopia (DM Ethio Lawyers)](https://dmethiolawyers.com/electronic-signature-in-ethiopia/)
- [Zoho Sign: Ethiopia](https://www.zoho.com/sign/electronic-signatures/legality/ethiopia.html)
- [BoldSign: Ethiopia](https://boldsign.com/electronic-signature-legality/ethiopia/)
- [Digital Policy Alert: Proclamation 1205/2020](https://digitalpolicyalert.org/event/27371-electronic-transactions-proclamation-no-12052020-including-data-protection-regulation-adopted-by-ethiopian-government)
- [Addis Ababa University thesis on 1205/2020](https://etd.aau.edu.et/handle/123456789/4344)
- [DLA Piper: data protection in Ethiopia](https://www.dlapiperdataprotection.com/?c=ET&t=law)
- [EUR-Lex: eIDAS consolidated text](https://eur-lex.europa.eu/eli/reg/2014/910/en/cons)
- [Freshfields: eIDAS 2.0](https://freshfields.com/en/our-thinking/campaigns/tech-data-and-ai-the-digital-frontier/eu-digital-strategy/eidas)

## 2. Terms used in the product

The UI, documentation and certificate must use these terms consistently and must never present
one as another.

| Term | Meaning in DocuMind Sign | Provided in the initial release |
|---|---|---|
| **Electronic signature** | Data that a person attaches to a document to show intent to sign: a drawn, typed or uploaded mark, plus the recorded consent and actions | **Yes** |
| **Digital signature** | A cryptographic signature over the document's bytes, made with a private key | **No** |
| **Certificate-backed digital signature (PAdES)** | A digital signature whose key is bound to an identity by a certificate from a certification authority | **No** (future, through pyHanko and a provider) |
| **Qualified or jurisdiction-specific signature** | For example eIDAS QES, or Ethiopia's "reliable" or "digital" categories, if confirmed. Requires accredited providers and devices | **No** |
| **Document integrity hash** | SHA-256 of the stored files, recorded at each step | **Yes.** It proves a file matches the recorded hash; it proves neither identity nor legal validity |
| **Completion certificate** | A PDF evidence summary generated by DocuMind | **Yes.** It is not a certificate issued by a government or trust-service provider |

**Wording rules for the UI and emails:**
- say "sign electronically";
- never say "digitally signed", "certified", "legally binding" or "court-admissible";
- the certificate footer states what it is (02 §9.2).

## 3. Engineering requirements

| Concern | Requirement | Where implemented (02) |
|---|---|---|
| **Intent to sign** | An explicit "Sign" action at the end; nothing pre-selected; the action's wording is recorded | §4.2 `/sign/complete`; events |
| **Consent to do business electronically** | A disclosure shown before any field can be filled; explicit acceptance; the disclosure version and time recorded. Per ESIGN, the consumer-consent rules must be reviewed if signers may be consumers | §4.2 `/sign/consent`; event `consent.accepted` |
| **Withdrawing consent and paper copies** | Content for the disclosure, and the process (contact the sender), to be defined with a lawyer | Disclosure text (M1, reviewable) |
| **Signer identification** | Email link (possession of the mailbox) as the minimum; an optional email code; in M2, a DocuMind session with the organization's 2FA. The method used is recorded per recipient. Stronger identity checks are out of scope | §13, §15 C3 |
| **Attribution** | Each signature tied to a recipient, the session, a server time, and the IP address and user agent | §9.1 |
| **Integrity** | Immutable snapshot; SHA-256 of source, snapshot, final and certificate; verification endpoint; hash-chained events; final PDF flattened | §6, §9 |
| **Keeping records** | Final PDF, certificate and events kept as long as the workspace exists, per C1; signers can download a copy after completion | §15 C1, §4.2 `/sign/download` |
| **Copy for signers** | A completion email with a link to download the final PDF and certificate; no attachments | §10 |
| **Accessibility** | The portal meets WCAG 2.2 AA (existing standard) | M1 to M2 |

## 4. Personal data

**Data subjects:**
- senders and internal recipients (DocuMind users);
- **external signers**, who aren't users and haven't accepted DocuMind's terms.

**Data processed about external signers:**
- name and email (entered by the sender);
- IP address and user agent (recorded at portal actions);
- signature images and typed signature text;
- field values;
- timestamps.

**Requirements for legal review:**
1. **Legal basis and roles.** The customer organization is presumably the controller and
   DocuMind the processor. The DPA and terms must cover signer data. To be confirmed.
2. **Transparency.** The portal shows a short privacy notice: who is collecting the data, what
   is recorded (including IP address) and why, with a link to the full notice.
3. **Minimisation.** IP address and user agent are recorded only at evidence events, never on
   page views generally. No device fingerprinting.
4. **Retention.** Linked to decision C1:
   - Option A deletes with the organization.
   - Option B, retention after deletion, needs a documented legal basis and a fixed period, and
     is **not** implemented without that.
5. **Rights requests.** Access, erasure and correction for external signers: there's no
   self-service. The process goes through the customer organization, with support from
   DocuMind operators. Erasing data from a completed agreement may conflict with keeping
   records, which is for the lawyer to decide.
6. **International transfers.** Workspaces can keep data in their own region and storage
   (existing infrastructure profiles). Signer data follows the workspace's data plane.
7. **Data export (ADR 0039).** A user's export lists the agreements they sent. External
   signers have no DocuMind account; their copy is the completion download.

## 5. Jurisdiction-specific assessment needed

- **Ethiopia:** confirm the relationship and scope of 1072/2018 and 1205/2020. Confirm whether
  the category this product provides (simple electronic signature with evidence) is enough for
  the target use cases, and which transactions are excluded. Check any licensing or
  accreditation requirements for signature service providers. Check the data protection law.
- **EU:** this product provides at most a simple electronic signature in eIDAS terms. Whether an
  advanced electronic signature (Art. 26) could be reached by a future identity and PAdES
  integration is a separate assessment. **No qualified claim.**
- **US:** ESIGN and UETA consumer-disclosure applicability for each customer use case, and
  record-retention expectations.
- **Exclusions in general:** many jurisdictions exclude some documents from electronic
  signature, such as wills, family law, some real-estate deeds and court documents. The product
  should state that customers are responsible for the suitability of their document types. The
  exact text is for the lawyer.

## 6. Third-party licences (compliance-adjacent)

| Component | Licence | Note |
|---|---|---|
| PyMuPDF (already in the product, text extraction) | AGPL-3.0 or Artifex commercial | **Existing product question, not introduced by Sign.** AGPL obligations can apply to software offered over a network. Decide: buy a commercial licence, accept AGPL obligations for the service, or replace it for extraction. Sign doesn't extend its use, except optionally for M3 word positions, which also has an MIT alternative (pdfminer.six) |
| pdfjs-dist | Apache-2.0 | Include its notice in the frontend's third-party notices |
| pikepdf (qpdf) | MPL-2.0 (qpdf: Apache-2.0) | Unmodified use; provide notices |
| reportlab | BSD | Notices |
| Pillow | MIT-CMU (HPND) | Notices |
| Signature font for typed signatures | must be OFL or similar | Chosen and recorded in M1 |

## 7. What the product must not claim (checklist for UI, documentation and marketing)

- ☐ No "legally binding" or "enforceable" without qualification.
- ☐ No "digital signature", "certified signature", "PAdES", "QES", "eIDAS-qualified" or
  "advanced electronic signature".
- ☐ No "compliant with Proclamation 1072/2018 / 1205/2020 / ESIGN / UETA" until a lawyer
  confirms.
- ☐ The completion certificate isn't described as issued by an authority.
- ☐ Hashes are described as integrity evidence, not as identity proof.
- ☐ AI contract analysis is labelled "not legal advice" wherever it appears.

## 8. Open items for a lawyer

1. Confirm the official texts and current versions of the instruments in §1, and identify
   missing ones: Ethiopian implementing directives, the data protection law, and sector rules
   such as banking.
2. Approve the consent and disclosure text and the privacy notice for signers.
3. Decide retention and deletion (C1), and handling of erasure requests for completed
   agreements.
4. Approve the wording rules in §2 and §7.
5. Advise on PyMuPDF licensing for the existing product.
