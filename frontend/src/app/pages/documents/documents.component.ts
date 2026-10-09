import { DatePipe } from '@angular/common';
import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { AuthService } from '../../core/services/auth.service';
import { CoherenceVerdict, DOC_STATUS_LABELS, DocumentMeta } from '../../core/models';

@Component({
  selector: 'app-documents',
  standalone: true,
  imports: [FormsModule, DatePipe],
  template: `
    <div class="header">
      <h2>Documenti</h2>
      <button class="btn" (click)="load()" [disabled]="loading()">Aggiorna</button>
    </div>
    <p class="desc">
      Ingestion documentale supervisionata dall'Agente di Coerenza: i documenti accettati
      vengono smistati nel RAG Principale o nel RAG Futuro / Roadmap.
    </p>

    @if (message(); as msg) {
      <div class="banner" [class.error]="msg.error">{{ msg.text }}</div>
    }

    @if (isScrumMaster) {
      <form class="upload-panel" (ngSubmit)="upload()">
        <div class="row">
          <label class="file">
            File (.md, .txt, .rst, .adoc — max 2 MB)
            <input type="file" accept=".md,.txt,.rst,.adoc" (change)="onFile($event)" />
          </label>
          <label>
            Titolo (opzionale)
            <input name="title" [(ngModel)]="title" placeholder="Es. Specifica modulo Billing" />
          </label>
          <label>
            RAG di destinazione
            <select name="rag" [(ngModel)]="targetRag">
              <option value="main">RAG Principale (già implementato)</option>
              <option value="future">RAG Futuro / Roadmap</option>
            </select>
          </label>
        </div>
        <button class="btn primary" type="submit" [disabled]="!file || uploading()">
          {{ uploading() ? 'Caricamento…' : 'Carica documento' }}
        </button>
      </form>
    } @else {
      <p class="hint">Il caricamento della documentazione è riservato allo Scrum Master.</p>
    }

    @if (loading()) {
      <p class="empty">Caricamento…</p>
    } @else if (documents().length === 0) {
      <p class="empty">Nessun documento caricato.</p>
    } @else {
      <table>
        <thead>
          <tr>
            <th>Titolo</th>
            <th>File</th>
            <th>RAG</th>
            <th>Stato</th>
            <th>Caricato</th>
            <th>File</th>
            @if (isScrumMaster) {
              <th>Decisione</th>
            }
          </tr>
        </thead>
        <tbody>
          @for (d of documents(); track d.id) {
            <tr>
              <td class="title">{{ d.title }}</td>
              <td class="mono">{{ d.file_name ?? '—' }}</td>
              <td><span class="badge rag-{{ d.target_rag }}">{{ d.target_rag === 'main' ? 'Principale' : 'Futuro' }}</span></td>
              <td><span class="badge st-{{ d.status }}">{{ statusLabel(d.status) }}</span></td>
              <td>{{ d.created_at | date: 'dd/MM/yy HH:mm' }}</td>
              <td>
                <button class="btn mini" (click)="download(d)" [disabled]="downloading() === d.id"
                        title="Scarica il contenuto del documento">
                  {{ downloading() === d.id ? '…' : '⬇ Scarica' }}
                </button>
              </td>
              @if (isScrumMaster) {
                <td class="row-actions">
                  @if (d.status === 'uploaded' || d.status === 'in_review' || d.status === 'validating') {
                    <button class="btn mini ai" (click)="checkCoherence(d)" [disabled]="checking() === d.id"
                            title="L'Agente di Coerenza confronta il documento con il repository">
                      {{ checking() === d.id ? 'Verifica…' : '✦ Verifica coerenza' }}
                    </button>
                    <button class="btn mini ok" (click)="decide(d, 'accept')" title="Accetta il documento">Accetta</button>
                    <button class="btn mini" (click)="decide(d, 'force')" title="Forza nonostante le incongruenze">Forza</button>
                    <button class="btn mini" (click)="decide(d, 'review')" title="Richiedi una revisione">Revisione</button>
                    <button class="btn mini ko" (click)="decide(d, 'reject')" title="Rifiuta il documento">Rifiuta</button>
                  } @else {
                    <span class="done">—</span>
                  }
                </td>
              }
            </tr>
            @if (verdicts()[d.id]; as v) {
              <tr>
                <td [attr.colspan]="isScrumMaster ? 7 : 6">
                  <div class="verdict" [class.ok]="v.coherent" [class.ko]="!v.coherent">
                    <div class="verdict-head">
                      <strong>
                        {{ v.coherent ? '✓ Documento coerente con il repository' : '⚠ Incongruenze rilevate' }}
                      </strong>
                      <span class="meta">
                        confidenza {{ v.confidence }}% · modello {{ v.model }} ·
                        topologia {{ v.topology_used ? 'usata' : 'non disponibile' }} ·
                        suggerimento: <strong>{{ decisionLabel(v.suggested_decision) }}</strong>
                      </span>
                    </div>
                    <p>{{ v.summary }}</p>
                    @if (v.issues.length > 0) {
                      <ul>
                        @for (issue of v.issues; track issue) {
                          <li>{{ issue }}</li>
                        }
                      </ul>
                    }
                  </div>
                </td>
              </tr>
            }
          }
        </tbody>
      </table>
    }
  `,
  styles: `
    :host { display: block; }
    h2 { margin: 0 0 0.25rem; font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; }
    .header { display: flex; align-items: center; justify-content: space-between; }
    .desc { color: var(--text-muted); font-size: 0.9rem; }
    .hint { color: var(--text-muted); font-style: italic; margin-top: 1rem; }
    .banner {
      margin-top: 1rem;
      background: var(--ok-soft);
      color: var(--ok);
      border-radius: var(--radius);
      padding: 0.6rem 1rem;
      font-size: 0.9rem;

      &.error { background: var(--err-soft); color: var(--err); }
    }
    .upload-panel {
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
      label { display: flex; flex-direction: column; gap: 0.3rem; font-size: 0.8rem; font-weight: 600; flex: 1; min-width: 220px; }
      input, select {
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
      &.mini { padding: 0.3rem 0.6rem; font-size: 0.78rem; }
      &.ok { border-color: var(--ok); color: var(--ok); &:hover { background: var(--ok-soft); } }
      &.ko { border-color: var(--err); color: var(--err); &:hover { background: var(--err-soft); } }
      &.ai { border-color: var(--ai); color: var(--ai); &:hover { background: var(--ai-soft); } }
    }
    .verdict {
      border-radius: var(--radius);
      padding: 0.75rem 1rem;
      font-size: 0.85rem;
      background: var(--surface-2);
      border-left: 4px solid var(--border-strong);

      &.ok { background: var(--ok-soft); border-left-color: var(--ok); }
      &.ko { background: var(--warn-soft); border-left-color: var(--err); }

      .verdict-head {
        display: flex;
        justify-content: space-between;
        flex-wrap: wrap;
        gap: 0.5rem;

        .meta { color: var(--text-muted); font-size: 0.78rem; }
      }
      p { margin: 0.5rem 0 0; }
      ul { margin: 0.5rem 0 0; padding-left: 1.25rem; }
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
      .mono { font-family: var(--mono); font-size: 0.8rem; }
      .title { max-width: 320px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .row-actions { display: flex; gap: 0.3rem; flex-wrap: wrap; }
      .done { color: var(--text-muted); }
    }
    .badge {
      border-radius: var(--radius-sm);
      padding: 0.15rem 0.5rem;
      font-size: 0.72rem;
      font-weight: 600;
      background: var(--surface-2);
      color: var(--text-muted);

      &.rag-main { background: var(--ok-soft); color: var(--ok); }
      &.rag-future { background: var(--warn-soft); color: var(--warn); }
      &.st-uploaded, &.st-validating { background: var(--warn-soft); color: var(--warn); }
      &.st-accepted, &.st-forced { background: var(--ok-soft); color: var(--ok); }
      &.st-rejected { background: var(--err-soft); color: var(--err); }
      &.st-in_review { background: var(--info-soft); color: var(--info); }
    }
  `,
})
export class DocumentsComponent implements OnInit {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);

  readonly documents = signal<DocumentMeta[]>([]);
  readonly loading = signal(false);
  readonly uploading = signal(false);
  readonly message = signal<{ text: string; error: boolean } | null>(null);
  readonly checking = signal<string | null>(null);
  readonly downloading = signal<string | null>(null);
  readonly verdicts = signal<Record<string, CoherenceVerdict>>({});

  file: File | null = null;
  title = '';
  targetRag: 'main' | 'future' = 'main';

  get isScrumMaster(): boolean {
    return this.auth.hasRole('scrum_master');
  }

  ngOnInit(): void {
    this.load();
  }

  statusLabel(status: string): string {
    return DOC_STATUS_LABELS[status] ?? status;
  }

  onFile(event: Event): void {
    this.file = (event.target as HTMLInputElement).files?.[0] ?? null;
  }

  load(): void {
    this.loading.set(true);
    this.api.get<DocumentMeta[]>('/validator/documents').subscribe({
      next: (rows) => {
        this.documents.set(rows);
        this.loading.set(false);
      },
      error: () => this.loading.set(false),
    });
  }

  upload(): void {
    if (!this.file) {
      return;
    }
    const form = new FormData();
    form.append('file', this.file);
    form.append('title', this.title);
    form.append('target_rag', this.targetRag);
    this.uploading.set(true);
    this.message.set(null);
    this.api.upload<DocumentMeta>('/validator/documents', form).subscribe({
      next: (doc) => {
        this.uploading.set(false);
        this.file = null;
        this.title = '';
        this.message.set({ text: `Documento "${doc.title}" caricato: in attesa di validazione`, error: false });
        this.load();
      },
      error: (err) => {
        this.uploading.set(false);
        this.message.set({ text: err.error?.detail ?? 'Errore nel caricamento', error: true });
      },
    });
  }

  decisionLabel(decision: string): string {
    return { accept: 'Accetta', review: 'Revisione', reject: 'Rifiuta' }[decision] ?? decision;
  }

  /** Scarica il contenuto testuale del documento come file locale (Markdown). */
  download(doc: DocumentMeta): void {
    this.downloading.set(doc.id);
    this.api.get<{ id: string; title: string; content: string }>(`/validator/documents/${doc.id}/content`).subscribe({
      next: (res) => {
        this.downloading.set(null);
        const blob = new Blob([res.content ?? ''], { type: 'text/markdown;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement('a');
        anchor.href = url;
        anchor.download = this.downloadFileName(doc);
        anchor.click();
        URL.revokeObjectURL(url);
      },
      error: (err) => {
        this.downloading.set(null);
        this.message.set({ text: err.error?.detail ?? 'Impossibile scaricare il documento', error: true });
      },
    });
  }

  private downloadFileName(doc: DocumentMeta): string {
    if (doc.file_name) {
      return doc.file_name;
    }
    const slug = doc.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/(^-|-$)/g, '');
    return `${slug || 'documento'}.md`;
  }

  checkCoherence(doc: DocumentMeta): void {
    this.checking.set(doc.id);
    this.api.post<CoherenceVerdict>('/validator/validate', { document_id: doc.id }).subscribe({
      next: (verdict) => {
        this.checking.set(null);
        this.verdicts.update((v) => ({ ...v, [doc.id]: verdict }));
        if (!verdict.coherent) {
          this.message.set({
            text: `Attenzione: l'Agente di Coerenza ha rilevato incongruenze in "${doc.title}". Puoi forzare, rifiutare o chiedere una revisione.`,
            error: true,
          });
        }
        this.load();
      },
      error: (err) => {
        this.checking.set(null);
        this.message.set({ text: err.error?.detail ?? 'Verifica di coerenza non disponibile', error: true });
      },
    });
  }

  decide(doc: DocumentMeta, decision: 'accept' | 'force' | 'reject' | 'review'): void {
    this.api.post<DocumentMeta>(`/validator/documents/${doc.id}/decision`, { decision }).subscribe({
      next: () => {
        this.message.set({ text: `Decisione registrata per "${doc.title}"`, error: false });
        this.load();
      },
      error: (err) =>
        this.message.set({ text: err.error?.detail ?? 'Errore nella decisione', error: true }),
    });
  }
}
