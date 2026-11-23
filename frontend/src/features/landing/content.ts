/**
 * Marketing copy and demo scenarios for the landing page.
 *
 * All companies, documents and figures in the scenarios are fictional and
 * exist only to illustrate the product. Replace them with real, approved
 * customer stories before using any of them as testimonials.
 */
import type { IconName } from './components/Icon'

export type DocumentType = 'PDF' | 'DOCX' | 'MD' | 'TXT'

export interface Citation {
  id: number
  document: string
  page: number
  excerpt: string
}

export type AnswerPart = { kind: 'text'; text: string } | { kind: 'cite'; id: number }

export interface Scenario {
  id: string
  team: string
  icon: IconName
  workspace: string
  documents: { name: string; type: DocumentType }[]
  question: string
  answer: AnswerPart[]
  citations: Citation[]
  headline: string
  before: string
  outcomes: string[]
}

/** Parses "Some claim [1] and another [2]." into text and citation parts. */
export function parseAnswer(source: string): AnswerPart[] {
  return source
    .split(/(\[\d+\])/)
    .filter(Boolean)
    .map((chunk) => {
      const match = /^\[(\d+)\]$/.exec(chunk)
      return match ? { kind: 'cite', id: Number(match[1]) } : { kind: 'text', text: chunk }
    })
}

export const SCENARIOS: Scenario[] = [
  {
    id: 'legal',
    team: 'Legal',
    icon: 'scale',
    workspace: 'Legal · Contracts',
    documents: [
      { name: 'Halcyon Logistics MSA', type: 'PDF' },
      { name: 'Brightline Supply Agreement', type: 'PDF' },
      { name: 'Vendor Policy 2026', type: 'DOCX' },
    ],
    question: 'Which supplier agreements renew automatically, and how much notice do we need to give?',
    answer: parseAnswer(
      'Two agreements renew automatically. The Halcyon MSA renews for one-year terms unless either party gives 90 days’ written notice [1]. The Brightline agreement renews monthly and can be ended with 30 days’ notice [2]. Your vendor policy requires Legal to review any renewal above $50,000 [3].',
    ),
    citations: [
      {
        id: 1,
        document: 'Halcyon Logistics MSA',
        page: 14,
        excerpt:
          'This Agreement shall renew automatically for successive one (1) year terms unless either party provides written notice of non-renewal at least ninety (90) days prior to expiry.',
      },
      {
        id: 2,
        document: 'Brightline Supply Agreement',
        page: 6,
        excerpt:
          'Following the Initial Term, this Agreement renews on a month-to-month basis and may be terminated by either party upon thirty (30) days’ notice.',
      },
      {
        id: 3,
        document: 'Vendor Policy 2026',
        page: 3,
        excerpt: 'Renewals with an annual value above $50,000 require review by the Legal team before notice deadlines.',
      },
    ],
    headline: 'Never miss a renewal or a notice deadline',
    before: 'Clause hunting across dozens of contracts, one PDF at a time.',
    outcomes: [
      'Ask about obligations across every agreement at once',
      'Jump straight to the clause and page behind each answer',
      'Spot policy conflicts before they become commitments',
    ],
  },
  {
    id: 'finance',
    team: 'Finance',
    icon: 'chart',
    workspace: 'Finance · Board reporting',
    documents: [
      { name: 'Q2 Board Report', type: 'PDF' },
      { name: 'Q3 Board Report', type: 'PDF' },
      { name: 'FY26 Budget Memo', type: 'DOCX' },
    ],
    question: 'Why did operating expenses rise between Q2 and Q3?',
    answer: parseAnswer(
      'Two drivers account for most of the increase. Cloud infrastructure costs grew after the EU region launch [1], and three senior engineering hires started in July [2]. The FY26 budget memo planned for the hires but not for the infrastructure increase [3].',
    ),
    citations: [
      {
        id: 1,
        document: 'Q3 Board Report',
        page: 8,
        excerpt: 'Infrastructure spend increased following the launch of the EU hosting region in August.',
      },
      {
        id: 2,
        document: 'Q3 Board Report',
        page: 11,
        excerpt: 'Headcount grew by three senior engineers, all of whom joined in July.',
      },
      {
        id: 3,
        document: 'FY26 Budget Memo',
        page: 2,
        excerpt: 'Approved headcount includes three senior engineering roles; infrastructure is held flat year over year.',
      },
    ],
    headline: 'Explain the numbers, not just report them',
    before: 'Cross-referencing board packs and memos to reconstruct the story behind a variance.',
    outcomes: [
      'Compare periods across reports in a single question',
      'Trace every figure back to the page it came from',
      'Surface where actuals and plans disagree',
    ],
  },
  {
    id: 'research',
    team: 'Research',
    icon: 'flask',
    workspace: 'R&D · Literature review',
    documents: [
      { name: 'Okafor et al. 2025', type: 'PDF' },
      { name: 'Lindqvist & Hale 2024', type: 'PDF' },
      { name: 'Moreau 2026', type: 'PDF' },
    ],
    question: 'What sample sizes did these studies use, and do their results agree?',
    answer: parseAnswer(
      'They used 120 [1], 480 [2] and 64 participants [3]. The two larger studies report a similar effect. The smallest found no significant effect, and its author suggests the sample was too small [3]. That is the author’s interpretation, not a confirmed finding.',
    ),
    citations: [
      { id: 1, document: 'Okafor et al. 2025', page: 4, excerpt: 'A total of 120 participants were enrolled across two sites.' },
      { id: 2, document: 'Lindqvist & Hale 2024', page: 3, excerpt: 'In total, 480 adults completed the full protocol.' },
      {
        id: 3,
        document: 'Moreau 2026',
        page: 9,
        excerpt: 'With only 64 participants, the study may have been underpowered to detect the effect reported elsewhere.',
      },
    ],
    headline: 'Synthesize literature without losing nuance',
    before: 'Reading every methods section to compare studies by hand.',
    outcomes: [
      'Compare methods and findings across papers',
      'See conflicting results called out explicitly',
      'Keep facts separate from the authors’ interpretation',
    ],
  },
  {
    id: 'people',
    team: 'People Ops',
    icon: 'users',
    workspace: 'People · Policies',
    documents: [
      { name: 'Employee Handbook 2026', type: 'PDF' },
      { name: 'Contractor Agreement', type: 'DOCX' },
      { name: 'Benefits Overview', type: 'MD' },
    ],
    question: 'Does our parental leave policy apply to contractors?',
    answer: parseAnswer(
      'Not according to these documents. The parental leave policy covers full-time and part-time employees [1], and the contractor agreement excludes contractors from employee benefits [2]. Neither mentions an exception, so confirm with HR before replying.',
    ),
    citations: [
      {
        id: 1,
        document: 'Employee Handbook 2026',
        page: 22,
        excerpt: 'Parental leave is available to all full-time and part-time employees after three months of service.',
      },
      {
        id: 2,
        document: 'Contractor Agreement',
        page: 4,
        excerpt: 'The Contractor is not an employee and is not eligible for employee benefit programs.',
      },
    ],
    headline: 'Give employees consistent, policy-backed answers',
    before: 'The same policy questions, answered differently by different people.',
    outcomes: [
      'Answer from the current handbook, every time',
      'Know when the documents don’t cover a case',
      'Share answers with the exact policy text attached',
    ],
  },
  {
    id: 'support',
    team: 'Support',
    icon: 'headset',
    workspace: 'Support · Knowledge base',
    documents: [
      { name: 'Router X2 User Manual', type: 'PDF' },
      { name: 'Troubleshooting Guide', type: 'MD' },
      { name: 'Release Notes 4.2', type: 'TXT' },
    ],
    question: 'A customer’s device won’t reconnect after the 4.2 update. What should they try?',
    answer: parseAnswer(
      'Release 4.2 changed the default Wi-Fi band to 5 GHz, which some older devices can’t join [1]. Ask the customer to enable “Dual-band compatibility” under Settings → Wireless [2]. If that fails, a network reset restores 4.1 defaults and keeps the update [3].',
    ),
    citations: [
      { id: 1, document: 'Release Notes 4.2', page: 1, excerpt: 'Default band changed from 2.4 GHz to 5 GHz for new and updated devices.' },
      {
        id: 2,
        document: 'Troubleshooting Guide',
        page: 7,
        excerpt: 'Enable Dual-band compatibility (Settings → Wireless) for clients that only support 2.4 GHz.',
      },
      {
        id: 3,
        document: 'Router X2 User Manual',
        page: 31,
        excerpt: 'A network reset restores default wireless settings without reverting firmware.',
      },
    ],
    headline: 'Resolve tickets with the right fix, first time',
    before: 'Searching manuals, release notes and old tickets while the customer waits.',
    outcomes: [
      'Find the fix across manuals and release notes at once',
      'Paste answers with sources your customers can check',
      'Onboard new agents without tribal knowledge',
    ],
  },
]

export const CAPABILITIES: { icon: IconName; title: string; detail: string }[] = [
  { icon: 'file', title: 'PDF, DOCX, TXT & Markdown', detail: 'Page-aware extraction' },
  { icon: 'quote', title: 'Page-level citations', detail: 'Document, page and excerpt' },
  { icon: 'layers', title: 'Multi-document answers', detail: 'Ask across a whole collection' },
  { icon: 'shield', title: 'Workspace isolation', detail: 'Enforced in every query' },
]

export const STEPS: { icon: IconName; title: string; body: string; meta: string }[] = [
  {
    icon: 'upload',
    title: 'Upload',
    body: 'Drop in contracts, reports, manuals or papers. Files are validated and stored privately.',
    meta: 'PDF · DOCX · TXT · MD',
  },
  {
    icon: 'cpu',
    title: 'Understand',
    body: 'Text is extracted with page numbers and headings, then split into meaningful passages.',
    meta: 'Section-aware chunking',
  },
  {
    icon: 'search',
    title: 'Retrieve',
    body: 'Your question is matched by meaning and keywords, only within documents you can access.',
    meta: 'Semantic + keyword search',
  },
  {
    icon: 'sparkles',
    title: 'Answer',
    body: 'A grounded answer streams back with citations to the exact pages it relied on.',
    meta: 'Cited, streamed, saved',
  },
]

export const SECURITY_POINTS: { title: string; body: string }[] = [
  { title: 'Tenant isolation by design', body: 'Every search and answer is filtered by workspace inside the database query itself.' },
  { title: 'Private storage', body: 'Files live in private buckets and are shared only through short-lived signed links.' },
  { title: 'Roles and permissions', body: 'Owner, Admin, Member and Viewer roles with explicit permission policies.' },
  { title: 'Content treated as untrusted', body: 'Instructions hidden inside documents are never followed as system instructions.' },
  { title: 'Audit trail', body: 'Membership, permission and document events are recorded per workspace.' },
  { title: 'Usage controls', body: 'Track storage, documents and AI usage, with limits you can configure.' },
]
