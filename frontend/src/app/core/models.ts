/** Modelli condivisi dell'applicazione. */

export interface User {
  id: number;
  username: string;
  email: string;
  domain: string | null;
  roles: string[];
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  user: User;
}

export interface Ticket {
  id: string;
  external_id: string | null;
  source: 'jira' | 'github' | 'bugzilla' | 'cve' | 'internal';
  type: 'bug' | 'cve' | 'change_request';
  title: string;
  description: string | null;
  severity: string | null;
  domain: string | null;
  status: string;
  priority: number | null;
  imported_at: string;
}

export interface WorkOrderTicket {
  id: string;
  title: string | null;
  status: string | null;
  domain: string | null;
}

export interface WorkOrder {
  id: string;
  status: string;
  tickets: WorkOrderTicket[];
  payload: Record<string, unknown>;
  changes: unknown;
  rationale: string | null;
  // Gestione Git
  repo_url: string | null;
  base_branch: string | null;
  work_branch: string | null;
  code_pr_url: string | null;
  code_pr_state: string;
  docs_pr_url: string | null;
  docs_pr_state: string;
  // Assegnazione allo sviluppatore (reviewer della PR)
  assignee_id: number | null;
  assignee_username: string | null;
  assignee_github: string | null;
  created_at: string;
  updated_at: string;
}

export interface RepoOption {
  url: string;
  name: string;
}

export interface Developer {
  id: number;
  username: string;
  domain: string | null;
  github_username: string | null;
}

export interface EligibleTicket {
  id: string;
  title: string;
  severity?: string | null;
  domain: string | null;
  status: string;
  priority?: number | null;
  current_wo_id?: string | null; // WO in cui si trova ora (se assegnato altrove)
}

export interface AdminUser {
  id: number;
  username: string;
  email: string;
  domain: string | null;
  github_username: string | null;
  is_active: boolean;
  roles: string[];
}

export interface ImportResult {
  status: string;
  found?: number;
  imported?: number;
  skipped_duplicates?: number;
  detail?: string;
}

export interface PlatformSetting {
  key: string;
  value: string;
  secret: boolean;
  configured: boolean;
}

export interface RepoCredential {
  repo_slug: string;
  repo_url: string;
  configured: boolean;
  updated_at?: string;
}

export interface DocumentMeta {
  id: string;
  title: string;
  file_name: string | null;
  target_rag: 'main' | 'future';
  status: string;
  created_at: string;
  updated_at: string;
}

/** Esito del test di connessione di un'integrazione (Impostazioni). */
export interface TestResult {
  ok: boolean;
  detail: string;
}

/** Esito di un singolo test eseguito dall'Agente Tester su un Work Order. */
export interface TestCaseResult {
  file: string;
  suite: string | null;
  name: string;
  status: 'passed' | 'failed' | 'skipped';
  /** 'new' = introdotto da questo Work Order; 'regression' = suite preesistente. */
  origin: 'new' | 'regression';
  duration_ms: number | null;
  message: string | null;
}

/** Riepilogo di un'esecuzione della suite di test su un Work Order. */
export interface TestRun {
  id: string;
  status: 'running' | 'passed' | 'failed' | 'no_tests' | 'error';
  branch: string | null;
  commit_sha: string | null;
  /** Valorizzato se si è ripiegato sul branch base (codice non ancora pubblicato). */
  fallback_reason: string | null;
  error_stage: string | null;
  error_detail: string | null;
  /** Esito del controllo dei tipi eseguito prima della suite. */
  typecheck_status: 'skipped' | 'passed' | 'failed';
  typecheck_output: string | null;
  /** Esito della produzione degli artefatti eseguibili. */
  build_status: 'skipped' | 'passed' | 'failed';
  build_output: string | null;
  total_tests: number;
  passed_tests: number;
  failed_tests: number;
  skipped_tests: number;
  new_total: number;
  new_failed: number;
  regression_total: number;
  regression_failed: number;
  duration_ms: number | null;
  started_at: string;
  finished_at: string | null;
}

export interface TestRunLatest {
  run: TestRun | null;
  cases: TestCaseResult[];
  lines: string[];
}

export interface WhatIfResult {
  what_if: boolean;
  model: string;
  topology_used: boolean;
  impact_percentage: number;
  affected_files: string[];
  risk_notes: string;
  decision: 'auto_approved' | 'needs_authorization' | 'blocked';
}

export interface CoherenceVerdict {
  document_id: string;
  model: string;
  topology_used: boolean;
  coherent: boolean;
  confidence: number;
  issues: string[];
  summary: string;
  suggested_decision: 'accept' | 'review' | 'reject';
  detail: string;
}

export const DOC_STATUS_LABELS: Record<string, string> = {
  uploaded: 'Caricato',
  validating: 'In validazione',
  accepted: 'Accettato',
  forced: 'Forzato',
  rejected: 'Rifiutato',
  in_review: 'In revisione',
  archived: 'Archiviato',
};

export const ROLE_LABELS: Record<string, string> = {
  product_manager: 'Product Manager',
  scrum_master: 'Scrum Master',
  developer: 'Sviluppatore',
};

export const TICKET_STATUS_LABELS: Record<string, string> = {
  raw: 'Grezzo',
  deduplicated: 'Deduplicato',
  prioritized: 'Prioritizzato',
  analyzed: 'Analizzato',
  approved: 'Approvato',
  rejected: 'Rifiutato',
  postponed: 'Posticipato',
  in_progress: 'In lavorazione',
  completed: 'Completato',
};

export const WO_STATUS_LABELS: Record<string, string> = {
  generated: 'Generato',
  running: 'In esecuzione',
  code_ready: 'Codice pronto',
  validated: 'Validato',
  merged: 'Unito al repo',
  failed: 'Fallito',
};
