import { Component, OnInit, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { ApiService } from '../../core/services/api.service';
import { AdminUser, PlatformSetting, RepoCredential, ROLE_LABELS, TestResult } from '../../core/models';

interface FieldDef {
  key: string;
  label: string;
  placeholder: string;
  secret?: boolean;
  multiline?: boolean;
}

interface SectionDef {
  title: string;
  description: string;
  testSource?: 'jira' | 'github' | 'bugzilla' | 'cve';
  fields: FieldDef[];
}

const SECTIONS: SectionDef[] = [
  {
    title: 'Jira',
    description: 'Importazione dei ticket e Reverse Sync verso Jira Cloud.',
    testSource: 'jira',
    fields: [
      { key: 'jira.base_url', label: 'Base URL', placeholder: 'https://azienda.atlassian.net' },
      { key: 'jira.email', label: 'Email account', placeholder: 'utente@azienda.com' },
      { key: 'jira.api_token', label: 'API token', placeholder: 'Token Atlassian', secret: true },
      { key: 'jira.project_key', label: 'Project key (Reverse Sync)', placeholder: 'es. SCRUM' },
    ],
  },
  {
    title: 'GitHub',
    description: 'Importazione delle issue dal repository. Token necessario solo per repo privati.',
    testSource: 'github',
    fields: [
      { key: 'github.owner', label: 'Owner / organizzazione', placeholder: 'es. my-org' },
      { key: 'github.repo', label: 'Repository', placeholder: 'es. my-app' },
      { key: 'github.token', label: 'Personal access token', placeholder: 'ghp_…', secret: true },
    ],
  },
  {
    title: 'Bugzilla',
    description: 'Importazione dei bug dal prodotto configurato. API key necessaria solo per istanze private.',
    testSource: 'bugzilla',
    fields: [
      { key: 'bugzilla.base_url', label: 'Base URL', placeholder: 'https://bugzilla.azienda.com' },
      { key: 'bugzilla.product', label: 'Prodotto', placeholder: 'es. Firefox' },
      { key: 'bugzilla.api_key', label: 'API key (opzionale)', placeholder: 'Chiave API Bugzilla', secret: true },
    ],
  },
  {
    title: 'CVE (NVD)',
    description: 'Importazione delle vulnerabilità CVE dal NVD/NIST, filtrate per parola chiave (es. la tecnologia aziendale). API key opzionale.',
    testSource: 'cve',
    fields: [
      { key: 'cve.keyword', label: 'Parola chiave', placeholder: 'es. spring, log4j, openssl' },
      { key: 'cve.api_key', label: 'API key NVD (opzionale)', placeholder: 'Chiave NVD', secret: true },
      { key: 'cve.base_url', label: 'Endpoint (opzionale)', placeholder: 'default: NVD 2.0' },
    ],
  },
  {
    title: 'Repository del software',
    description: 'Il/i repository Git su cui operano Agente Mappatore e Sviluppatore. Salvando, topologia e grafo del codice vengono ricostruiti automaticamente. Per progetti multi-repo inserisci un URL per riga.',
    fields: [
      { key: 'repo.git_url', label: 'URL Git (uno per riga per multi-repo)', placeholder: 'https://github.com/my-org/app-backend.git\nhttps://github.com/my-org/app-frontend.git', multiline: true },
      { key: 'repo.branch', label: 'Branch', placeholder: 'main' },
    ],
  },
];

@Component({
  selector: 'app-settings',
  standalone: true,
  imports: [FormsModule],
  template: `
    <h2>Impostazioni</h2>
    <p class="desc">
      Credenziali e integrazioni della piattaforma. I segreti sono conservati lato server
      e mai mostrati in chiaro.
    </p>

    @if (message(); as msg) {
      <div class="banner" [class.error]="msg.error">{{ msg.text }}</div>
    }

    @for (section of sections; track section.title) {
      <section class="panel">
        <div class="panel-head">
          <div>
            <h3>{{ section.title }}</h3>
            <p>{{ section.description }}</p>
          </div>
          @if (section.testSource; as src) {
            <button class="btn" (click)="test(src)" [disabled]="testing() === src">
              {{ testing() === src ? 'Test in corso…' : 'Test connessione' }}
            </button>
          }
        </div>

        @if (section.testSource && testResults()[section.testSource!]; as result) {
          <div class="test-result" [class.ok]="result.ok" [class.ko]="!result.ok">
            {{ result.ok ? '✓' : '✗' }} {{ result.detail }}
          </div>
        }

        <div class="grid">
          @for (field of section.fields; track field.key) {
            <label>
              {{ field.label }}
              @if (configured()[field.key]) {
                <span class="dot" title="Configurato">●</span>
              }
              @if (field.multiline) {
                <textarea
                  rows="3"
                  [placeholder]="field.placeholder"
                  [name]="field.key"
                  [ngModel]="values()[field.key] ?? ''"
                  (ngModelChange)="setValue(field.key, $event)"
                ></textarea>
              } @else {
                <input
                  [type]="field.secret ? 'password' : 'text'"
                  [placeholder]="field.placeholder"
                  [name]="field.key"
                  [ngModel]="values()[field.key] ?? ''"
                  (ngModelChange)="setValue(field.key, $event)"
                  autocomplete="off"
                />
              }
            </label>
          }
        </div>
      </section>
    }

    <button class="btn primary save" (click)="save()" [disabled]="saving()">
      {{ saving() ? 'Salvataggio…' : 'Salva impostazioni' }}
    </button>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>Token Git per-repository</h3>
          <p>
            Token GitHub dedicato per singolo repository (permessi minimi), cifrato
            a riposo. Ha la precedenza sul token globale; lascia vuoto per rimuoverlo.
          </p>
        </div>
      </div>
      <div class="repo-cred-form">
        <input [(ngModel)]="newRepoUrl" name="newRepoUrl"
               placeholder="URL repository (es. https://github.com/org/app.git)" />
        <input [(ngModel)]="newRepoToken" name="newRepoToken" type="password"
               placeholder="Personal access token (ghp_…)" />
        <button class="btn primary" (click)="saveRepoToken()" [disabled]="savingRepoCred()">
          {{ savingRepoCred() ? 'Salvo…' : 'Salva token' }}
        </button>
      </div>
      @if (repoCreds().length > 0) {
        <table class="users">
          <thead><tr><th>Repository</th><th>Token</th><th></th></tr></thead>
          <tbody>
            @for (c of repoCreds(); track c.repo_slug) {
              <tr>
                <td><strong>{{ c.repo_slug }}</strong></td>
                <td><span class="role-badge">configurato •••</span></td>
                <td><button class="btn mini" (click)="removeRepoToken(c)">Rimuovi</button></td>
              </tr>
            }
          </tbody>
        </table>
      } @else {
        <p class="muted-p">Nessun token dedicato: tutti i repository usano il token globale GitHub.</p>
      }
    </section>

    <section class="panel">
      <div class="panel-head">
        <div>
          <h3>Utenti, domini e GitHub</h3>
          <p>
            Assegna gli Sviluppatori ai domini del software (es. billing, hr) e imposta
            il loro handle GitHub, usato come reviewer/assignee delle Pull Request.
          </p>
        </div>
      </div>
      <table class="users">
        <thead>
          <tr>
            <th>Utente</th>
            <th>Email</th>
            <th>Ruoli</th>
            <th>Dominio</th>
            <th>GitHub</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          @for (u of users(); track u.id) {
            <tr>
              <td><strong>{{ u.username }}</strong></td>
              <td>{{ u.email }}</td>
              <td>
                @for (r of u.roles; track r) {
                  <span class="role-badge">{{ roleLabel(r) }}</span>
                }
              </td>
              <td>
                <input
                  [name]="'domain-' + u.id"
                  [ngModel]="userDomains()[u.id] ?? ''"
                  (ngModelChange)="setUserDomain(u.id, $event)"
                  placeholder="es. billing, hr"
                  class="domain-input"
                />
              </td>
              <td>
                <input
                  [name]="'github-' + u.id"
                  [ngModel]="userGithubs()[u.id] ?? ''"
                  (ngModelChange)="setUserGithub(u.id, $event)"
                  placeholder="handle GitHub"
                  class="domain-input"
                />
              </td>
              <td>
                <button
                  class="btn mini"
                  (click)="saveUser(u)"
                  [disabled]="savingDomain() === u.id || !userChanged(u)"
                >
                  {{ savingDomain() === u.id ? '…' : 'Salva' }}
                </button>
              </td>
            </tr>
          }
        </tbody>
      </table>
    </section>
  `,
  styles: `
    :host { display: block; }
    h2 { margin: 0 0 0.25rem; font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; }
    .desc { color: var(--text-muted); font-size: 0.9rem; }
    .banner {
      margin: 1rem 0;
      background: var(--ok-soft);
      color: var(--ok);
      border-radius: var(--radius);
      padding: 0.6rem 1rem;
      font-size: 0.9rem;

      &.error { background: var(--err-soft); color: var(--err); }
    }
    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      box-shadow: var(--shadow-1);
      margin-top: 1.25rem;
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 1rem;

      h3 { margin: 0; }
      p { margin: 0.25rem 0 0; color: var(--text-muted); font-size: 0.85rem; }
    }
    .test-result {
      margin-top: 0.75rem;
      border-radius: var(--radius);
      padding: 0.5rem 0.9rem;
      font-size: 0.85rem;
      font-weight: 600;

      &.ok { background: var(--ok-soft); color: var(--ok); }
      &.ko { background: var(--err-soft); color: var(--err); }
    }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 0.9rem;
      margin-top: 1rem;
    }
    label {
      display: flex;
      flex-direction: column;
      gap: 0.3rem;
      font-size: 0.8rem;
      font-weight: 600;

      .dot { color: var(--ok); margin-left: 0.25rem; }
      input, textarea {
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 0.5rem 0.7rem;
        font-size: 0.9rem;
        font-family: inherit;
        resize: vertical;
        &:focus { outline: none; border-color: var(--ai); box-shadow: 0 0 0 3px var(--ai-soft); }
      }
    }
    .btn {
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--text);
      border-radius: var(--radius);
      padding: 0.5rem 0.9rem;
      font-size: 0.85rem;
      font-weight: 600;
      cursor: pointer;
      white-space: nowrap;
      font-family: inherit;

      &:hover { background: var(--surface-2); }
      &:disabled { opacity: 0.5; cursor: default; }
      &.primary { background: var(--primary); border-color: var(--primary); color: var(--primary-contrast); &:hover { background: var(--primary-hover); } }
    }
    .save { margin-top: 1.5rem; }
    .repo-cred-form { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-top: 0.5rem;
      input { flex: 1; min-width: 180px; padding: 0.4rem 0.6rem; border-radius: var(--radius); border: 1px solid var(--border); background: var(--surface); color: var(--text); } }
    .muted-p { color: var(--text-muted); font-style: italic; margin-top: 0.75rem; }
    .users {
      width: 100%;
      border-collapse: collapse;
      margin-top: 1rem;

      th, td { padding: 0.55rem 0.6rem; text-align: left; font-size: 0.85rem; }
      th { color: var(--text-muted); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.05em; }
      tr + tr td { border-top: 1px solid var(--border); }
    }
    .role-badge {
      background: var(--ai-soft);
      color: var(--ai);
      border-radius: var(--radius-sm);
      padding: 0.15rem 0.5rem;
      font-size: 0.72rem;
      font-weight: 600;
      margin-right: 0.3rem;
    }
    .domain-input {
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 0.35rem 0.6rem;
      font-size: 0.85rem;
      width: 150px;
      font-family: inherit;
      &:focus { outline: none; border-color: var(--ai); box-shadow: 0 0 0 3px var(--ai-soft); }
    }
    .btn.mini { padding: 0.3rem 0.7rem; font-size: 0.78rem; }
  `,
})
export class SettingsComponent implements OnInit {
  private readonly api = inject(ApiService);

  readonly sections = SECTIONS;
  readonly values = signal<Record<string, string>>({});
  readonly configured = signal<Record<string, boolean>>({});
  readonly saving = signal(false);
  readonly testing = signal<string | null>(null);
  readonly testResults = signal<Record<string, TestResult>>({});
  readonly message = signal<{ text: string; error: boolean } | null>(null);

  readonly users = signal<AdminUser[]>([]);
  readonly userDomains = signal<Record<number, string>>({});
  readonly userGithubs = signal<Record<number, string>>({});
  readonly savingDomain = signal<number | null>(null);

  // token Git per-repository (cifrati a riposo lato backend)
  readonly repoCreds = signal<RepoCredential[]>([]);
  readonly savingRepoCred = signal(false);
  newRepoUrl = '';
  newRepoToken = '';

  ngOnInit(): void {
    this.load();
    this.loadUsers();
    this.loadRepoCreds();
  }

  loadRepoCreds(): void {
    this.api.get<RepoCredential[]>('/ingestion/repo-credentials').subscribe({
      next: (rows) => this.repoCreds.set(rows),
      error: () => this.repoCreds.set([]),
    });
  }

  saveRepoToken(): void {
    if (!this.newRepoUrl.trim() || !this.newRepoToken.trim()) {
      this.message.set({ text: 'Inserisci URL del repository e token', error: true });
      return;
    }
    this.savingRepoCred.set(true);
    this.api
      .put('/ingestion/repo-credentials', { repo_url: this.newRepoUrl.trim(), token: this.newRepoToken.trim() })
      .subscribe({
        next: () => {
          this.savingRepoCred.set(false);
          this.newRepoUrl = '';
          this.newRepoToken = '';
          this.message.set({ text: 'Token del repository salvato (cifrato)', error: false });
          this.loadRepoCreds();
        },
        error: (err) => {
          this.savingRepoCred.set(false);
          this.message.set({ text: err.error?.detail ?? 'Errore nel salvataggio del token', error: true });
        },
      });
  }

  removeRepoToken(cred: RepoCredential): void {
    this.api.put('/ingestion/repo-credentials', { repo_url: cred.repo_url, token: '' }).subscribe({
      next: () => {
        this.message.set({ text: `Token rimosso per ${cred.repo_slug}`, error: false });
        this.loadRepoCreds();
      },
      error: (err) => this.message.set({ text: err.error?.detail ?? 'Errore nella rimozione', error: true }),
    });
  }

  setValue(key: string, value: string): void {
    this.values.update((v) => ({ ...v, [key]: value }));
  }

  roleLabel(role: string): string {
    return ROLE_LABELS[role] ?? role;
  }

  setUserDomain(userId: number, value: string): void {
    this.userDomains.update((d) => ({ ...d, [userId]: value }));
  }

  setUserGithub(userId: number, value: string): void {
    this.userGithubs.update((g) => ({ ...g, [userId]: value }));
  }

  /** True se dominio o handle GitHub sono stati modificati rispetto al salvato. */
  userChanged(user: AdminUser): boolean {
    const domainChanged = (this.userDomains()[user.id] ?? '') !== (user.domain ?? '');
    const githubChanged = (this.userGithubs()[user.id] ?? '') !== (user.github_username ?? '');
    return domainChanged || githubChanged;
  }

  loadUsers(): void {
    this.api.get<AdminUser[]>('/auth/users').subscribe({
      next: (rows) => {
        this.users.set(rows);
        const domains: Record<number, string> = {};
        const githubs: Record<number, string> = {};
        for (const u of rows) {
          domains[u.id] = u.domain ?? '';
          githubs[u.id] = u.github_username ?? '';
        }
        this.userDomains.set(domains);
        this.userGithubs.set(githubs);
      },
      error: () => this.message.set({ text: 'Impossibile caricare gli utenti', error: true }),
    });
  }

  /** Salva dominio e/o handle GitHub (solo i campi effettivamente cambiati). */
  saveUser(user: AdminUser): void {
    this.savingDomain.set(user.id);
    const calls: Promise<unknown>[] = [];
    if ((this.userDomains()[user.id] ?? '') !== (user.domain ?? '')) {
      const domain = (this.userDomains()[user.id] ?? '').trim() || null;
      calls.push(firstValueFrom(this.api.put(`/auth/users/${user.id}/domain`, { domain })));
    }
    if ((this.userGithubs()[user.id] ?? '') !== (user.github_username ?? '')) {
      const github_username = (this.userGithubs()[user.id] ?? '').trim() || null;
      calls.push(firstValueFrom(this.api.put(`/auth/users/${user.id}/github`, { github_username })));
    }
    Promise.all(calls)
      .then(() => {
        this.savingDomain.set(null);
        this.message.set({ text: `Profilo di ${user.username} aggiornato`, error: false });
        this.loadUsers();
      })
      .catch((err) => {
        this.savingDomain.set(null);
        this.message.set({ text: err?.error?.detail ?? 'Errore nel salvataggio', error: true });
      });
  }

  load(): void {
    this.api.get<PlatformSetting[]>('/ingestion/settings').subscribe({
      next: (rows) => {
        const values: Record<string, string> = {};
        const configured: Record<string, boolean> = {};
        for (const row of rows) {
          values[row.key] = row.value;
          configured[row.key] = row.configured;
        }
        this.values.set(values);
        this.configured.set(configured);
      },
      error: () => this.message.set({ text: 'Impossibile caricare le impostazioni', error: true }),
    });
  }

  save(): void {
    this.saving.set(true);
    this.message.set(null);
    this.api.put<{ updated: number; repo_rescan?: boolean }>('/ingestion/settings', { settings: this.values() }).subscribe({
      next: (res) => {
        this.saving.set(false);
        this.message.set({
          text: res.repo_rescan
            ? 'Impostazioni salvate. Ricostruzione di topologia e grafo del codice avviata (può richiedere qualche minuto).'
            : 'Impostazioni salvate',
          error: false,
        });
        this.load();
      },
      error: (err) => {
        this.saving.set(false);
        this.message.set({ text: err.error?.detail ?? 'Errore nel salvataggio', error: true });
      },
    });
  }

  test(source: 'jira' | 'github' | 'bugzilla' | 'cve'): void {
    this.testing.set(source);
    // Salva prima i valori correnti, così il test usa ciò che vedi nel form
    this.api.put('/ingestion/settings', { settings: this.values() }).subscribe({
      next: () => {
        this.api.post<TestResult>(`/ingestion/settings/test/${source}`, {}).subscribe({
          next: (result) => {
            this.testing.set(null);
            this.testResults.update((r) => ({ ...r, [source]: result }));
            this.load();
          },
          error: () => {
            this.testing.set(null);
            this.testResults.update((r) => ({
              ...r,
              [source]: { ok: false, detail: 'Errore durante il test' },
            }));
          },
        });
      },
      error: () => {
        this.testing.set(null);
        this.message.set({ text: 'Errore nel salvataggio prima del test', error: true });
      },
    });
  }
}
