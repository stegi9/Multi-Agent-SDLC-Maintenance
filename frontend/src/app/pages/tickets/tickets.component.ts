import { DatePipe } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { AuthService } from '../../core/services/auth.service';
import { ProjectService } from '../../core/services/project.service';
import { ImportResult, TICKET_STATUS_LABELS, Ticket, WhatIfResult } from '../../core/models';

@Component({
  selector: 'app-tickets',
  standalone: true,
  imports: [FormsModule, DatePipe],
  template: `
    <div class="header">
      <h2>Ticket</h2>
      <div class="actions">
        @if (projects.hasProjects()) {
          <select class="proj-select" [ngModel]="projects.activeRepo()" (ngModelChange)="changeProject($event)"
                  name="activeProject" title="Progetto attivo (repository)">
            @for (r of projects.repos(); track r.url) {
              <option [value]="r.url">📦 {{ r.name }}</option>
            }
          </select>
        }
        <button class="btn secondary" (click)="load()" [disabled]="loading()">Aggiorna</button>
        <button class="btn" (click)="importJira()" [disabled]="importing()">
          {{ importing() ? 'Importazione…' : 'Importa da Jira' }}
        </button>
        @if (canCreateWo) {
          <button class="btn agent" (click)="planWorkOrders()" [disabled]="planning()"
                  title="Un agente analizza i ticket approvati e crea i Work Order raggruppandoli">
            {{ planning() ? 'Pianificazione…' : '⚙ Crea Work Order (agente)' }}
          </button>
        }
        @if (canWhatIf) {
          <button class="btn whatif-btn" (click)="showWhatIf.set(!showWhatIf())">
            {{ showWhatIf() ? 'Chiudi What-If' : '⚡ What-If Analysis' }}
          </button>
        }
        @if (canCreate) {
          <button class="btn primary" (click)="showCreate.set(!showCreate())">
            {{ showCreate() ? 'Annulla' : '+ Nuovo Bug / CR' }}
          </button>
        }
      </div>
    </div>

    @if (message(); as msg) {
      <div class="banner" [class.error]="msg.error">{{ msg.text }}</div>
    }

    @if (showWhatIf()) {
      <div class="create-panel whatif">
        <h3>⚡ What-If Analysis</h3>
        <p class="hint-text">
          Simula l'impatto di una bozza di modifica <strong>senza creare ticket</strong> e senza
          toccare i tracker ufficiali (Jira, GitHub). L'Agente di Planning stima impatto e rischi.
        </p>
        <label>
          Descrizione della modifica ipotetica
          <textarea name="whatIfDescription" [(ngModel)]="whatIfDescription" rows="3"
                    placeholder="Es. Cambiare i colori dell'interfaccia in giallo e verde"></textarea>
        </label>
        <button class="btn primary" (click)="runWhatIf()"
                [disabled]="analyzing() || whatIfDescription.trim().length < 10">
          {{ analyzing() ? 'Analisi in corso…' : 'Analizza impatto' }}
        </button>

        @if (whatIfResult(); as r) {
          <div class="whatif-result" [class.low]="r.decision === 'auto_approved'"
               [class.mid]="r.decision === 'needs_authorization'"
               [class.high]="r.decision === 'blocked'">
            <div class="impact-row">
              <span class="impact-pct">{{ r.impact_percentage }}%</span>
              <div>
                <strong>{{ decisionLabel(r.decision) }}</strong>
                <span class="meta"> · modello {{ r.model }} · topologia {{ r.topology_used ? 'usata' : 'non disponibile' }}</span>
              </div>
            </div>
            @if (r.risk_notes) {
              <p><strong>Rischi:</strong> {{ r.risk_notes }}</p>
            }
            @if (r.affected_files?.length) {
              <p class="meta">File potenzialmente coinvolti: {{ r.affected_files.join(', ') }}</p>
            }
          </div>
        }
      </div>
    }

    @if (showCreate()) {
      <form class="create-panel" (ngSubmit)="createTicket()">
        <div class="row">
          <label>
            Tipo
            <select name="type" [(ngModel)]="form.type">
              <option value="bug">Bug</option>
              <option value="change_request">Change Request</option>
            </select>
          </label>
          <label>
            Severity
            <select name="severity" [(ngModel)]="form.severity">
              <option value="">-</option>
              <option>Low</option>
              <option>Medium</option>
              <option>High</option>
              <option>Critical</option>
            </select>
          </label>
          <label>
            Dominio
            <input name="domain" [(ngModel)]="form.domain"
                   placeholder="es. billing, hr (opzionale)" />
          </label>
        </div>
        <label>
          Titolo
          <input name="title" [(ngModel)]="form.title" required minlength="3" maxlength="500"
                 placeholder="Es. Cambiare i colori dell'interfaccia in giallo e verde" />
        </label>
        <label>
          Descrizione
          <textarea name="description" [(ngModel)]="form.description" rows="3"
                    placeholder="Dettagli del bug o della change request"></textarea>
        </label>
        <button class="btn primary" type="submit" [disabled]="creating() || form.title.length < 3">
          {{ creating() ? 'Creazione…' : 'Crea ticket' }}
        </button>
      </form>
    }

    @if (loading()) {
      <p class="empty">Caricamento…</p>
    } @else if (tickets().length === 0) {
      <p class="empty">
        Nessun ticket presente. Importa da Jira o crea un nuovo Bug / Change Request.
      </p>
    } @else {
      <table>
        <thead>
          <tr>
            <th>Sorgente</th>
            <th>ID esterno</th>
            <th>Tipo</th>
            <th>Titolo</th>
            <th>Severity</th>
            <th>Dominio</th>
            <th>Stato</th>
            <th>Importato</th>
            <th>Azioni</th>
          </tr>
        </thead>
        <tbody>
          @for (t of tickets(); track t.id) {
            <tr>
              <td><span class="badge src-{{ t.source }}">{{ t.source }}</span></td>
              <td>{{ t.external_id ?? '—' }}</td>
              <td>{{ t.type === 'change_request' ? 'CR' : t.type }}</td>
              <td class="title" [title]="t.description ?? ''">{{ t.title }}</td>
              <td>{{ t.severity ?? '—' }}</td>
              <td>
                @if (t.domain) {
                  <span class="badge dom">{{ t.domain }}</span>
                } @else { — }
              </td>
              <td><span class="badge st-{{ t.status }}">{{ statusLabel(t.status) }}</span></td>
              <td>{{ t.imported_at | date: 'dd/MM/yy HH:mm' }}</td>
              <td class="row-actions">
                @if (t.source === 'internal' && !t.external_id) {
                  <button class="btn mini" (click)="reverseSync(t)" title="Crea l'issue su Jira (Reverse Sync)">
                    → Jira
                  </button>
                }
                @if (canCreateWo && (t.status === 'approved' || t.status === 'analyzed')) {
                  <button class="btn mini ko" (click)="rejectTicket(t)"
                          title="Rifiuta il ticket: genera il Report degli Scarti">
                    Rifiuta
                  </button>
                }
              </td>
            </tr>
          }
        </tbody>
      </table>
    }
  `,
  styles: `
    :host { display: block; }
    h2 { margin: 0 0 0.25rem; font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; }
    .header { display: flex; align-items: flex-start; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem; }
    .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; align-items: center; }
    .proj-select { padding: 0.4rem 0.55rem; border-radius: var(--radius); border: 1px solid var(--ai); background: var(--ai-soft); color: var(--ai); font-weight: 600; }
    .btn {
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: var(--radius);
      padding: 0.5rem 0.9rem;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      font-family: inherit;

      &:hover { background: var(--surface-2); }
      &:disabled { opacity: 0.5; cursor: default; }
      &.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-contrast); &:hover { background: var(--primary-hover); } }
      &.secondary { color: var(--text-muted); }
      &.ko { border-color: var(--err); color: var(--err); &:hover { background: var(--err-soft); } }
      &.mini { padding: 0.25rem 0.6rem; font-size: 0.75rem; }
    }
    .banner {
      margin-top: 1rem;
      background: var(--ok-soft);
      color: var(--ok);
      border-radius: var(--radius);
      padding: 0.6rem 1rem;
      font-size: 0.9rem;

      &.error { background: var(--err-soft); color: var(--err); }
    }
    .create-panel {
      margin-top: 1rem;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      box-shadow: var(--shadow-1);
      display: flex;
      flex-direction: column;
      gap: 0.75rem;

      .row { display: flex; gap: 1rem; flex-wrap: wrap; }
      label { display: flex; flex-direction: column; gap: 0.3rem; font-size: 0.8rem; font-weight: 600; flex: 1; min-width: 160px; }
      input, select, textarea {
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 0.5rem 0.7rem;
        font-size: 0.9rem;
        font-family: inherit;
        &:focus { outline: none; border-color: var(--ai); box-shadow: 0 0 0 3px var(--ai-soft); }
      }
      .btn { align-self: flex-start; }
    }
    .empty { margin-top: 2rem; color: var(--text-muted); }
    .whatif-btn { border-color: var(--ai); color: var(--ai); &:hover { background: var(--ai-soft); } }
    .btn.agent { background: var(--ai); border-color: var(--ai); color: #fff; &:hover { background: var(--ai-hover); } }
    .whatif {
      border-left: 3px solid var(--ai);
      h3 { margin: 0; }
      .hint-text { margin: 0; color: var(--text-muted); font-size: 0.85rem; }
    }
    .whatif-result {
      border-radius: var(--radius);
      padding: 0.9rem 1.1rem;
      background: var(--surface-2);
      border-left: 4px solid var(--border-strong);
      font-size: 0.88rem;

      &.low { background: var(--ok-soft); border-left-color: var(--ok); }
      &.mid { background: var(--warn-soft); border-left-color: var(--warn); }
      &.high { background: var(--err-soft); border-left-color: var(--err); }

      .impact-row { display: flex; align-items: center; gap: 0.9rem; }
      .impact-pct { font-size: 1.6rem; font-weight: 700; }
      .meta { color: var(--text-muted); font-size: 0.8rem; }
      p { margin: 0.5rem 0 0; }
    }
    table {
      margin-top: 1.25rem;
      width: 100%;
      border-collapse: collapse;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
      box-shadow: var(--shadow-1);

      th, td { padding: 0.65rem 0.85rem; text-align: left; font-size: 0.85rem; }
      th { background: var(--surface-2); color: var(--text-muted); font-size: 0.72rem;
        text-transform: uppercase; letter-spacing: 0.05em; }
      tr + tr td { border-top: 1px solid var(--border); }
      tbody tr:hover td { background: var(--surface-alt); }
      .title { max-width: 360px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .row-actions { display: flex; gap: 0.35rem; }
    }
    .badge {
      border-radius: var(--radius-sm);
      padding: 0.15rem 0.5rem;
      font-size: 0.72rem;
      font-weight: 600;
      background: var(--surface-2);
      color: var(--text-muted);

      &.src-jira { background: var(--info-soft); color: var(--info); }
      &.src-internal { background: var(--ok-soft); color: var(--ok); }
      &.src-bugzilla { background: var(--warn-soft); color: var(--warn); }
      &.src-cve { background: var(--err-soft); color: var(--err); }
      &.dom { background: var(--ai-soft); color: var(--ai); }
      &.st-raw, &.st-prioritized, &.st-deduplicated { background: var(--warn-soft); color: var(--warn); }
      &.st-analyzed, &.st-approved { background: var(--ai-soft); color: var(--ai); }
      &.st-in_progress { background: var(--info-soft); color: var(--info); }
      &.st-completed { background: var(--ok-soft); color: var(--ok); }
      &.st-rejected, &.st-postponed { background: var(--err-soft); color: var(--err); }
    }
  `,
})
export class TicketsComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  readonly projects = inject(ProjectService);

  readonly tickets = signal<Ticket[]>([]);
  readonly loading = signal(false);
  readonly importing = signal(false);
  readonly creating = signal(false);
  readonly showCreate = signal(false);
  readonly showWhatIf = signal(false);
  readonly planning = signal(false);
  readonly analyzing = signal(false);
  readonly whatIfResult = signal<WhatIfResult | null>(null);
  readonly message = signal<{ text: string; error: boolean } | null>(null);

  whatIfDescription = '';

  form = {
    type: 'bug' as 'bug' | 'change_request',
    title: '',
    description: '',
    severity: '',
    domain: '',
  };

  /** PM e Scrum Master creano ticket; lo Scrum Master autorizza i WO. */
  get canCreate(): boolean {
    return this.auth.hasRole('product_manager') || this.auth.hasRole('scrum_master');
  }

  /** La What-If Analysis è consultiva (non crea ticket): disponibile a tutti i ruoli, Sviluppatore incluso. */
  get canWhatIf(): boolean {
    return this.canCreate || this.auth.hasRole('developer');
  }

  get canCreateWo(): boolean {
    return this.auth.hasRole('scrum_master');
  }

  ngOnInit(): void {
    this.projects.loadRepos();
    this.load();
  }

  changeProject(repoUrl: string): void {
    this.projects.setActive(repoUrl);
    this.load();
  }

  statusLabel(status: string): string {
    return TICKET_STATUS_LABELS[status] ?? status;
  }

  decisionLabel(decision: string): string {
    return (
      {
        auto_approved: 'Impatto basso: rientrerebbe nella procedura rapida (<5%)',
        needs_authorization: 'Impatto medio: servirebbe l’autorizzazione dello Scrum Master (5–30%)',
        blocked: 'Impatto alto: verrebbe posticipato con Report degli Scarti (>30%)',
      }[decision] ?? decision
    );
  }

  runWhatIf(): void {
    this.analyzing.set(true);
    this.whatIfResult.set(null);
    this.api
      .post<WhatIfResult>('/impact/what-if', { description: this.whatIfDescription.trim() })
      .subscribe({
        next: (result) => {
          this.analyzing.set(false);
          this.whatIfResult.set(result);
        },
        error: (err) => {
          this.analyzing.set(false);
          this.notify(err.error?.detail ?? "Analisi What-If non disponibile (LLM raggiungibile?)", true);
        },
      });
  }

  private notify(text: string, error = false): void {
    this.message.set({ text, error });
    setTimeout(() => this.message.set(null), 6000);
  }

  load(): void {
    this.loading.set(true);
    const repo = this.projects.activeRepo();
    const q = repo ? `?repo_url=${encodeURIComponent(repo)}` : '';
    this.api.get<Ticket[]>(`/ingestion/tickets${q}`).subscribe({
      next: (rows) => {
        this.tickets.set(rows);
        this.loading.set(false);
      },
      error: () => {
        this.loading.set(false);
        this.notify('Impossibile caricare i ticket', true);
      },
    });
  }

  importJira(): void {
    this.importing.set(true);
    const repo = this.projects.activeRepo();
    const q = repo ? `?repo_url=${encodeURIComponent(repo)}` : '';
    this.api.post<ImportResult>(`/ingestion/import/run${q}`, {}).subscribe({
      next: (res) => {
        this.importing.set(false);
        this.notify(
          `Import completato: ${res.imported ?? 0} nuovi, ${res.skipped_duplicates ?? 0} duplicati ignorati (trovati ${res.found ?? 0})`,
        );
        this.load();
      },
      error: (err) => {
        this.importing.set(false);
        this.notify(err.error?.detail ?? "Errore durante l'importazione", true);
      },
    });
  }

  createTicket(force = false): void {
    this.creating.set(true);
    const body = {
      ...this.form,
      severity: this.form.severity || null,
      domain: this.form.domain || null,
      repo_url: this.projects.activeRepo() || null,
      force,
    };
    this.api.post<Ticket>('/ingestion/tickets', body).subscribe({
      next: (t) => {
        this.creating.set(false);
        this.showCreate.set(false);
        this.form = { type: 'bug', title: '', description: '', severity: '', domain: '' };
        this.notify(`Ticket "${t.title}" creato`);
        this.load();
      },
      error: (err) => {
        this.creating.set(false);
        const detail = err.error?.detail;
        const match = detail?.roadmap_match;
        // Verifica pre-ingestion: la richiesta esiste già nel RAG Futuro
        if (err.status === 409 && match) {
          const pct = match.score != null ? ` (similarità ${Math.round(match.score * 100)}%)` : '';
          const ok = window.confirm(
            `Esiste già una specifica simile nel RAG Futuro:\n"${match.title}"${pct}.\n\n` +
              'Potrebbe essere un task duplicato. Crearlo comunque?',
          );
          if (ok) {
            this.createTicket(true);
          }
          return;
        }
        this.notify(
          typeof detail === 'string' ? detail : detail?.message ?? 'Errore nella creazione del ticket',
          true,
        );
      },
    });
  }

  reverseSync(t: Ticket): void {
    this.api.post<{ status: string; external_id: string }>(`/ingestion/reverse-sync/${t.id}`, {}).subscribe({
      next: (res) => {
        this.notify(`Reverse Sync completata: issue ${res.external_id} creata su Jira`);
        this.load();
      },
      error: (err) => this.notify(err.error?.detail ?? 'Errore nella Reverse Sync', true),
    });
  }

  planWorkOrders(): void {
    this.planning.set(true);
    this.api.post<{ created: number; llm?: boolean }>('/workorders/plan', {}).subscribe({
      next: (res) => {
        this.planning.set(false);
        if (res.created > 0) {
          this.notify(
            `L'agente ha creato ${res.created} Work Order raggruppando i ticket approvati` +
              (res.llm === false ? ' (euristica: LLM non disponibile)' : ''),
          );
        } else {
          this.notify('Nessun ticket approvato da raggruppare in Work Order', true);
        }
        this.load();
      },
      error: (err) => {
        this.planning.set(false);
        this.notify(err.error?.detail ?? 'Errore nella pianificazione dei Work Order', true);
      },
    });
  }

  rejectTicket(t: Ticket): void {
    const note = window.prompt(`Motivazione del rifiuto di "${t.title}" (opzionale):`, '');
    if (note === null) {
      return; // l'utente ha annullato
    }
    this.api.post<{ detail: string }>('/impact/reject', { ticket_id: t.id, note }).subscribe({
      next: () => {
        this.notify(`Ticket "${t.title}" rifiutato: generato il Report degli Scarti`);
        this.load();
      },
      error: (err) => this.notify(err.error?.detail ?? 'Errore nel rifiuto del ticket', true),
    });
  }
}
