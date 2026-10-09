import { DatePipe, JsonPipe } from '@angular/common';
import { Component, OnDestroy, OnInit, computed, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ApiService } from '../../core/services/api.service';
import { AuthService } from '../../core/services/auth.service';
import { ProjectService } from '../../core/services/project.service';
import {
  Developer,
  EligibleTicket,
  RepoOption,
  TestCaseResult,
  TestRun,
  TestRunLatest,
  WO_STATUS_LABELS,
  WhatIfResult,
  WorkOrder,
} from '../../core/models';

interface ExecutionLog {
  wo_id: string;
  status: string;
  lines: string[];
}

interface CodeChange {
  file: string;
  action: string;
  description: string;
  code_sketch?: string;
}

type Panel = 'review' | 'whatif' | 'terminal' | 'payload' | 'handoff' | 'tests' | null;

@Component({
  selector: 'app-work-orders',
  standalone: true,
  imports: [DatePipe, JsonPipe, FormsModule],
  template: `
    <div class="header">
      <h2>Work Order</h2>
      <div class="actions">
        @if (projects.hasProjects()) {
          <select class="proj-select" [ngModel]="projects.activeRepo()" (ngModelChange)="changeProject($event)"
                  name="activeProject" title="Progetto attivo (repository)">
            @for (r of projects.repos(); track r.url) {
              <option [value]="r.url">📦 {{ r.name }}</option>
            }
          </select>
        }
        @if (myDomain) {
          <label class="domain-filter">
            <input type="checkbox" [ngModel]="onlyMyDomain()" (ngModelChange)="onlyMyDomain.set($event)" />
            Solo il mio dominio (<strong>{{ myDomain }}</strong>)
          </label>
        }
        <button class="btn" (click)="load()" [disabled]="loading()">Aggiorna</button>
      </div>
    </div>
    <p class="desc">
      Ogni Work Order raggruppa uno o più ticket (creati dall'Agente di Pianificazione dalla
      pagina Ticket). Clicca una riga per aprire il dettaglio e le azioni.
    </p>

    @if (message(); as msg) {
      <div class="banner" [class.error]="msg.error">{{ msg.text }}</div>
    }

    @if (loading()) {
      <p class="empty">Caricamento…</p>
    } @else if (visible().length === 0) {
      <p class="empty">
        @if (onlyMyDomain() && myDomain) {
          Nessun Work Order per il dominio "{{ myDomain }}". Disattiva il filtro per vederli tutti.
        } @else {
          Nessun Work Order. Crealo dalla pagina Ticket con "⚙ Crea Work Order (agente)".
        }
      </p>
    } @else {
      <table>
        <thead>
          <tr>
            <th>ID</th>
            <th>Ticket</th>
            <th>Stato</th>
            <th>Creato</th>
          </tr>
        </thead>
        <tbody>
          @for (wo of visible(); track wo.id) {
            <tr class="clickable" (click)="open(wo)">
              <td class="mono">{{ wo.id.slice(0, 8) }}…</td>
              <td>
                <span class="count">{{ wo.tickets.length }} ticket</span>
                <span class="preview">{{ ticketsPreview(wo) }}</span>
              </td>
              <td><span class="badge st-{{ wo.status }}">{{ statusLabel(wo.status) }}</span></td>
              <td>{{ wo.created_at | date: 'dd/MM/yy HH:mm' }}</td>
            </tr>
          }
        </tbody>
      </table>
    }

    @if (selected(); as wo) {
      <div class="overlay" (click)="close()">
        <div class="modal" (click)="$event.stopPropagation()">
          <div class="modal-head">
            <div>
              <h3>Work Order <span class="mono">{{ wo.id.slice(0, 8) }}</span></h3>
              <span class="badge st-{{ wo.status }}">{{ statusLabel(wo.status) }}</span>
            </div>
            <button class="close" (click)="close()" title="Chiudi">×</button>
          </div>

          @if (wo.rationale) {
            <p class="rationale">↳ {{ wo.rationale }}</p>
          }

          <h4>Ticket coinvolti ({{ wo.tickets.length }})</h4>
          <ul class="grp">
            @for (t of wo.tickets; track t.id) {
              <li>
                <span class="t-title">{{ t.title }}</span>
                @if (t.domain) { <span class="badge dom">{{ t.domain }}</span> }
                <span class="badge st-{{ t.status }}">{{ t.status }}</span>
                @if (canEdit && isEditable(wo)) {
                  <button class="x" (click)="removeTicket(wo, t.id)" title="Rimuovi dal Work Order">×</button>
                }
              </li>
            }
            @if (wo.tickets.length === 0) {
              <li class="muted">Nessun ticket (il Work Order verrà eliminato se resta vuoto)</li>
            }
          </ul>
          @if (canEdit && isEditable(wo)) {
            <button class="btn add-btn" (click)="togglePicker()">
              {{ showPicker() ? 'Chiudi' : '+ Aggiungi ticket' }}
            </button>
            @if (showPicker()) {
              <div class="picker">
                @if (assignable().length === 0) {
                  <p class="muted">Nessun ticket disponibile da aggiungere.</p>
                } @else {
                  @for (e of assignable(); track e.id) {
                    <button class="pick-row" (click)="addTicket(wo, e.id)">
                      <span class="t-title">{{ e.title }}</span>
                      @if (e.domain) { <span class="badge dom">{{ e.domain }}</span> }
                      @if (e.current_wo_id) {
                        <span class="badge moved">nel WO {{ e.current_wo_id.slice(0, 8) }} → sposta</span>
                      } @else {
                        <span class="badge st-{{ e.status }}">{{ e.status }}</span>
                      }
                    </button>
                  }
                }
              </div>
            }
          }

          <h4>Repository Git</h4>
          <div class="repo-box">
            @if (wo.repo_url) {
              <div class="repo-current">
                <span class="badge repo">📦 {{ repoName(wo.repo_url) }}</span>
                @if (wo.base_branch) { <span class="branch">↳ {{ wo.base_branch }}</span> }
              </div>
              <div class="pr-states">
                <span class="pr-chip pr-{{ wo.code_pr_state }}">
                  Codice: {{ prLabel(wo.code_pr_state) }}
                  @if (wo.code_pr_url) { · <a [href]="wo.code_pr_url" target="_blank" rel="noopener">PR</a> }
                </span>
                <span class="pr-chip pr-{{ wo.docs_pr_state }}">
                  Docs: {{ prLabel(wo.docs_pr_state) }}
                  @if (wo.docs_pr_url) { · <a [href]="wo.docs_pr_url" target="_blank" rel="noopener">PR</a> }
                </span>
              </div>
            } @else {
              <p class="muted">Nessun repository assegnato a questo Work Order.</p>
            }

            @if (canAssignRepo(wo)) {
              <div class="repo-assign">
                @if (repos().length === 0) {
                  <p class="muted">Nessun repository configurato: aggiungilo in Impostazioni (repo.git_url).</p>
                } @else {
                  <select [(ngModel)]="repoChoice" name="repoChoice">
                    <option value="">— scegli repository —</option>
                    @for (r of repos(); track r.url) {
                      <option [value]="r.url">{{ r.name }}</option>
                    }
                  </select>
                  <input type="text" name="baseBranch" [(ngModel)]="baseBranch"
                         placeholder="branch base (opz., es. develop)" />
                  <button class="btn primary" (click)="assignRepo(wo)" [disabled]="assigningRepo()">
                    {{ assigningRepo() ? 'Assegno…' : (wo.repo_url ? 'Aggiorna repo' : 'Assegna repo') }}
                  </button>
                }
              </div>
            }
          </div>

          <h4>Sviluppatore assegnato</h4>
          <div class="repo-box">
            @if (wo.assignee_username) {
              <div class="repo-current">
                <span class="badge dev">👤 {{ wo.assignee_username }}</span>
                @if (wo.assignee_github) {
                  <span class="branch">GitHub: {{ wo.assignee_github }}</span>
                } @else {
                  <span class="muted warn">nessun handle GitHub → non sarà reviewer della PR</span>
                }
              </div>
            } @else {
              <p class="muted">Nessuno sviluppatore assegnato: la PR non avrà un reviewer designato.</p>
            }

            @if (canAssignRepo(wo)) {
              <div class="repo-assign">
                @if (developers().length === 0) {
                  <p class="muted">Nessuno sviluppatore configurato (utenti con ruolo 'developer').</p>
                } @else {
                  <select [(ngModel)]="devChoice" name="devChoice">
                    <option [ngValue]="null">— nessuno —</option>
                    @for (d of developers(); track d.id) {
                      <option [ngValue]="d.id">{{ d.username }}{{ d.github_username ? ' (' + d.github_username + ')' : '' }}</option>
                    }
                  </select>
                  <button class="btn primary" (click)="assignDeveloper(wo)" [disabled]="assigningDev()">
                    {{ assigningDev() ? 'Assegno…' : 'Assegna sviluppatore' }}
                  </button>
                }
              </div>
            }
          </div>

          <h4>Azioni</h4>
          <div class="action-bar">
            @if (canExecute && (wo.status === 'generated' || wo.status === 'failed')) {
              <button class="btn run" (click)="executeWo(wo)" [disabled]="executing() || !!execBlocked(wo)"
                      [title]="execBlocked(wo) || 'Avvia elaborazione del Work Order (Agente Sviluppatore)'">
                {{ executing() ? 'Avvio…' : (wo.status === 'failed' ? '↻ Riavvia elaborazione' : '▶ Avvia elaborazione') }}
              </button>
            }
            @if (canExecute && wo.status === 'code_ready') {
              <button class="btn run" (click)="executeWo(wo, true)" [disabled]="executing() || !!execBlocked(wo)"
                      [title]="execBlocked(wo) || 'Scarta il risultato e rielabora il Work Order'">
                {{ executing() ? 'Avvio…' : '↻ Rigenera' }}
              </button>
            }
            @if (canExecute && execBlocked(wo) && (wo.status === 'generated' || wo.status === 'failed' || wo.status === 'code_ready')) {
              <span class="await err">🚫 {{ execBlocked(wo) }}</span>
            }
            @if (canValidate && wo.code_pr_state === 'open') {
              <button class="btn" (click)="syncPr(wo)" [disabled]="syncing()"
                      title="Controlla su GitHub se la PR è stata fusa o chiusa a mano">
                {{ syncing() ? 'Controllo…' : '🔄 Aggiorna stato PR' }}
              </button>
            }
            @if (canValidate && wo.status === 'code_ready') {
              @if (wo.code_pr_state === 'open') {
                @if (wo.code_pr_url) {
                  <a class="btn ghlink" [href]="wo.code_pr_url" target="_blank" rel="noopener"
                     title="Rivedi il codice reale su GitHub prima di validare">🔍 Rivedi PR su GitHub</a>
                }
                <button class="btn review" (click)="panel.set(panel() === 'review' ? null : 'review')">✓ Valida codice</button>
              } @else if (wo.code_pr_state === 'error') {
                <span class="await err">⚠ L'agente non è riuscito ad aprire la PR — Rigenera</span>
              } @else {
                <span class="await">⏳ In attesa che l'agente apra la Pull Request su GitHub…</span>
              }
            }
            @if (canRunTests) {
              <button class="btn tests" (click)="runTests(wo)"
                      [disabled]="startingTests() || !!testsBlocked(wo)"
                      [title]="testsBlocked(wo) || 'Esegue i test sul branch di lavoro: quelli nuovi e la suite di regressione'">
                {{ startingTests() ? 'Avvio…' : '🧪 Esegui i test' }}
              </button>
              <button class="btn tests-ghost" (click)="toggleTests(wo)"
                      title="Mostra l'esito dell'ultima esecuzione dei test">
                {{ panel() === 'tests' ? 'Chiudi esito test' : '📋 Esito dei test' }}
              </button>
              @if (testsBlocked(wo)) {
                <span class="await err">🚫 {{ testsBlocked(wo) }}</span>
              }
              <!-- Compare solo dopo un rifiuto per branch mancante: l'agente cloud
                   non ha ancora pubblicato il codice, ma la suite esistente si può
                   comunque eseguire per verificare che nulla sia rotto. -->
              @if (branchMissing()) {
                <button class="btn tests-ghost" (click)="runTests(wo, true)" [disabled]="startingTests()"
                        title="Esegue la sola suite preesistente sul branch base">
                  ♻️ Esegui solo la regressione
                </button>
              }
            }
            <button class="btn whatif" (click)="runWhatIf(wo)" [disabled]="analyzing()">
              {{ analyzing() ? 'Analisi…' : '⚡ What-If' }}
            </button>
            <button class="btn term" (click)="toggleTerminal(wo)">
              {{ panel() === 'terminal' ? 'Chiudi terminale' : '▶ Terminale' }}
            </button>
            <button class="btn handoff-btn" (click)="showHandoff(wo)">📤 JSON per provider</button>
            <button class="btn" (click)="panel.set(panel() === 'payload' ? null : 'payload')">Payload JSON</button>
            @if (canEdit && isEditable(wo)) {
              <button class="btn ko" (click)="deleteWo(wo)">🗑 Elimina Work Order</button>
            }
          </div>

          @if (panel() === 'review') {
            <div class="review-panel">
              @if (asChanges(wo.changes); as ch) {
                <p class="summary">{{ ch.summary }}</p>
                <ul class="changes">
                  @for (c of ch.changes; track $index) {
                    <li>
                      <span class="act act-{{ c.action }}">{{ c.action }}</span>
                      <code>{{ c.file }}</code>
                      <div class="cd">{{ c.description }}</div>
                    </li>
                  }
                </ul>
              } @else {
                <p class="summary">Nessuna modifica registrata.</p>
              }
              <div class="review-actions">
                <button class="btn ok" (click)="validate(wo, true)" [disabled]="validating()">
                  {{ validating() ? 'Validazione…' : '✓ Approva e unisci (merge)' }}
                </button>
                <button class="btn ko" (click)="validate(wo, false)" [disabled]="validating()">✗ Rifiuta</button>
                <span class="hint">Se approvi: merge + documentazione automatica + normalizzazione del RAG.</span>
              </div>
            </div>
          }

          @if (panel() === 'whatif' && impact(); as r) {
            <div class="whatif-result" [class.low]="r.decision === 'auto_approved'"
                 [class.mid]="r.decision === 'needs_authorization'" [class.high]="r.decision === 'blocked'">
              <div class="impact-row">
                <span class="impact-pct">{{ r.impact_percentage }}%</span>
                <div>
                  <strong>{{ decisionLabel(r.decision) }}</strong>
                  <span class="meta"> · {{ r.model }} · topologia {{ r.topology_used ? 'usata' : 'non disponibile' }}</span>
                </div>
              </div>
              @if (r.risk_notes) { <p><strong>Rischi:</strong> {{ r.risk_notes }}</p> }
              @if (r.affected_files?.length) { <p class="meta">File: {{ r.affected_files.join(', ') }}</p> }
            </div>
          }

          @if (panel() === 'terminal') {
            <div class="terminal">
              <div class="term-bar">
                <span class="dots"><i></i><i></i><i></i></span>
                <span class="term-title">reasoning · {{ wo.id.slice(0, 8) }} · {{ statusLabel(terminalStatus()) }}</span>
                @if (terminalLive()) { <span class="live">● LIVE</span> }
              </div>
              <pre class="term-body">@for (line of terminalLines(); track $index) {<span class="ln">{{ line }}</span>
}@if (terminalLines().length === 0) {<span class="ln dim">In attesa del reasoning…</span>}</pre>
            </div>
          }

          @if (panel() === 'tests') {
            <div class="tests-panel">
              @if (testRun(); as r) {
                <div class="test-head" [class.ok]="r.status === 'passed'"
                     [class.ko]="r.status === 'failed'"
                     [class.warn]="r.status === 'error' || r.status === 'no_tests'">
                  <span class="verdict">{{ testVerdict(r) }}</span>
                  <span class="chips">
                    <span class="chip ok">{{ r.passed_tests }} passati</span>
                    <span class="chip ko">{{ r.failed_tests }} falliti</span>
                    @if (r.skipped_tests) { <span class="chip sk">{{ r.skipped_tests }} saltati</span> }
                  </span>
                  <span class="meta">
                    branch <code>{{ r.branch }}</code>
                    @if (r.duration_ms) { · {{ (r.duration_ms / 1000).toFixed(1) }} s }
                    @if (testLive()) { <span class="live">● IN CORSO</span> }
                  </span>
                </div>

                @if (r.fallback_reason === 'work_branch_missing') {
                  <p class="test-note">
                    Il branch di lavoro non era disponibile: è stata eseguita la sola
                    suite di regressione sul branch base.
                  </p>
                }
                @if (r.status === 'error') {
                  <p class="test-note err">
                    Esecuzione interrotta durante {{ testStageLabel(r.error_stage) }}.
                    Il dettaglio è nel terminale qui sotto.
                  </p>
                }
                @if (r.status === 'no_tests') {
                  <p class="test-note err">
                    Nessun test eseguito: l'agente non ne ha prodotti, oppure non
                    seguono la convenzione del repository.
                  </p>
                }

                <!-- Compilazione: risponde a una domanda che i test non pongono,
                     cioè se il codice consegnato compili affatto. -->
                @if (r.typecheck_status !== 'skipped' || r.build_status !== 'skipped') {
                  <div class="test-group">
                    <h5>🔧 Compilazione
                      <span class="tally">{{ compilazioneEsito(r) }}</span>
                    </h5>
                    <ul class="test-list">
                      @if (r.typecheck_status !== 'skipped') {
                        <li [class.failed]="r.typecheck_status === 'failed'">
                          <span class="st st-{{ r.typecheck_status }}">{{ statusIcon(r.typecheck_status) }}</span>
                          <div class="tc">
                            <div class="tname">Controllo dei tipi</div>
                            <div class="tfile">verifica che il codice consegnato sia coerente con i tipi dichiarati</div>
                            @if (r.typecheck_output) { <pre class="tmsg">{{ r.typecheck_output }}</pre> }
                          </div>
                        </li>
                      }
                      @if (r.build_status !== 'skipped') {
                        <li [class.failed]="r.build_status === 'failed'">
                          <span class="st st-{{ r.build_status }}">{{ statusIcon(r.build_status) }}</span>
                          <div class="tc">
                            <div class="tname">Produzione degli artefatti</div>
                            <div class="tfile">verifica che il progetto si impacchetti in eseguibili distribuibili</div>
                            @if (r.build_output) { <pre class="tmsg">{{ r.build_output }}</pre> }
                          </div>
                        </li>
                      }
                    </ul>
                  </div>
                }

                <!-- I test nati da questo Work Order: si mostrano tutti, perché
                     sono pochi ed è ciò che l'intervento ha aggiunto. -->
                <div class="test-group">
                  <h5>🆕 Test del Work Order
                    <span class="tally">{{ r.new_total - r.new_failed }}/{{ r.new_total }}</span>
                  </h5>
                  @if (r.new_total === 0) {
                    <p class="muted">Questo Work Order non ha introdotto test propri.</p>
                  } @else {
                    <ul class="test-list">
                      @for (c of casesFor('new'); track $index) {
                        <li [class.failed]="c.status === 'failed'" [class.skipped]="c.status === 'skipped'">
                          <span class="st st-{{ c.status }}">{{ statusIcon(c.status) }}</span>
                          <div class="tc">
                            <div class="tname">{{ c.suite ? c.suite + ' › ' : '' }}{{ c.name }}</div>
                            <div class="tfile"><code>{{ c.file }}</code></div>
                            @if (c.message) { <pre class="tmsg">{{ c.message }}</pre> }
                          </div>
                        </li>
                      }
                    </ul>
                  }
                </div>

                <!-- La suite preesistente: se ne mostrano SOLO i falliti. Sono
                     centinaia, e la domanda a cui deve rispondere è una sola:
                     l'intervento ha rotto qualcosa? -->
                <div class="test-group">
                  <h5>♻️ Suite di regressione
                    <span class="tally">{{ r.regression_total - r.regression_failed }}/{{ r.regression_total }}</span>
                  </h5>
                  @if (r.regression_total === 0) {
                    <p class="muted">Nessun test preesistente eseguito.</p>
                  } @else if (r.regression_failed === 0) {
                    <p class="muted ok-text">Nessuna regressione: tutti i test preesistenti passano.</p>
                  } @else {
                    <ul class="test-list">
                      @for (c of casesFor('regression'); track $index) {
                        <li class="failed">
                          <span class="st st-failed">✗</span>
                          <div class="tc">
                            <div class="tname">{{ c.suite ? c.suite + ' › ' : '' }}{{ c.name }}</div>
                            <div class="tfile"><code>{{ c.file }}</code></div>
                            @if (c.message) { <pre class="tmsg">{{ c.message }}</pre> }
                          </div>
                        </li>
                      }
                    </ul>
                  }
                </div>
              } @else {
                <p class="muted">Nessuna esecuzione dei test per questo Work Order.</p>
              }

              <!-- L'installazione delle dipendenze dura minuti: senza il terminale
                   l'utente non ha modo di distinguere un'attesa da un blocco. -->
              <div class="terminal">
                <div class="term-bar">
                  <span class="dots"><i></i><i></i><i></i></span>
                  <span class="term-title">test · {{ wo.id.slice(0, 8) }} · {{ testRun()?.status ?? '—' }}</span>
                  @if (testLive()) { <span class="live">● LIVE</span> }
                </div>
                <pre class="term-body">@for (line of testLines(); track $index) {<span class="ln">{{ line }}</span>
}@if (testLines().length === 0) {<span class="ln dim">Nessun log disponibile…</span>}</pre>
              </div>
            </div>
          }

          @if (panel() === 'payload') {
            <pre class="payload">{{ wo.payload | json }}</pre>
          }

          @if (panel() === 'handoff') {
            <div class="handoff-bar">
              <span>JSON di handoff completo — da inviare agli agenti cloud esterni</span>
              <button class="btn mini" (click)="copyHandoff()">Copia</button>
              <button class="btn mini" (click)="downloadHandoff(wo)">Scarica .json</button>
            </div>
            <pre class="payload">{{ handoffText() }}</pre>
          }
        </div>
      </div>
    }
  `,
  styles: `
    :host { display: block; }
    h2 { margin: 0 0 0.25rem; font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; }
    .header { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 0.5rem; }
    .actions { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap; }
    .proj-select { padding: 0.4rem 0.55rem; border-radius: var(--radius); border: 1px solid var(--ai); background: var(--ai-soft); color: var(--ai); font-weight: 600; }
    .domain-filter { display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem; color: var(--ai);
      background: var(--ai-soft); border-radius: var(--radius); padding: 0.4rem 0.75rem; cursor: pointer; }
    .desc { color: var(--text-muted); font-size: 0.9rem; }
    .banner { margin-top: 0.75rem; background: var(--ok-soft); color: var(--ok); border-radius: var(--radius);
      padding: 0.6rem 1rem; font-size: 0.9rem; &.error { background: var(--err-soft); color: var(--err); } }
    .empty { margin-top: 2rem; color: var(--text-muted); }
    .btn {
      border: 1px solid var(--border); background: var(--surface); color: var(--text);
      border-radius: var(--radius); padding: 0.5rem 0.9rem; font-family: inherit;
      font-size: 0.85rem; font-weight: 600; cursor: pointer;
      &:hover { background: var(--surface-2); }
      &:disabled { opacity: 0.5; cursor: default; }
      &.mini { padding: 0.3rem 0.6rem; font-size: 0.78rem; }
      &.whatif { border-color: var(--ai); color: var(--ai); &:hover { background: var(--ai-soft); } }
      &.term { border-color: var(--sidebar); color: var(--sidebar); &:hover { background: var(--sidebar); color: #fff; } }
      &.run { background: var(--info); border-color: var(--info); color: #fff; &:hover { filter: brightness(0.95); } }
      &.review { border-color: var(--ok); color: var(--ok); &:hover { background: var(--ok-soft); } }
      &.ok { background: var(--ok); border-color: var(--ok); color: #fff; &:hover { filter: brightness(0.95); } }
      &.ko { border-color: var(--err); color: var(--err); &:hover { background: var(--err-soft); } }
      &.ghlink { border-color: var(--ai); color: var(--ai); text-decoration: none; &:hover { background: var(--ai-soft); } }
    }
    .await { display: inline-flex; align-items: center; font-size: 0.82rem; color: var(--text-muted);
      font-style: italic; padding: 0.4rem 0.2rem;
      &.err { color: var(--warn); font-style: normal; } }
    table {
      margin-top: 1rem; width: 100%; border-collapse: collapse; background: var(--surface);
      border: 1px solid var(--border); border-radius: var(--radius-lg);
      overflow: hidden; box-shadow: var(--shadow-1);
      th, td { padding: 0.7rem 0.85rem; text-align: left; font-size: 0.85rem; }
      th { background: var(--surface-2); color: var(--text-muted); font-size: 0.72rem;
        text-transform: uppercase; letter-spacing: 0.05em; }
      tr + tr td { border-top: 1px solid var(--border); }
      .mono { font-family: var(--mono); }
      .clickable { cursor: pointer; &:hover td { background: var(--surface-alt); } }
      .count { display: inline-block; background: var(--ai); color: #fff;
        border-radius: var(--radius-sm); font-size: 0.72rem; font-weight: 700; padding: 0.1rem 0.55rem; margin-right: 0.5rem; }
      .preview { color: var(--text-muted); }
    }
    .badge { border-radius: var(--radius-sm); padding: 0.15rem 0.5rem; font-size: 0.72rem; font-weight: 600;
      background: var(--surface-2); color: var(--text-muted);
      &.dom { background: var(--ai-soft); color: var(--ai); }
      &.st-generated { background: var(--warn-soft); color: var(--warn); }
      &.st-running { background: var(--info-soft); color: var(--info); }
      &.st-code_ready, &.st-validated, &.st-merged, &.st-completed { background: var(--ok-soft); color: var(--ok); }
      &.st-failed { background: var(--err-soft); color: var(--err); }
      &.st-in_progress { background: var(--info-soft); color: var(--info); }
      &.st-approved, &.st-analyzed { background: var(--ai-soft); color: var(--ai); }
    }
    /* modal */
    .overlay { position: fixed; inset: 0; background: rgb(15 23 42 / 50%); display: flex;
      align-items: flex-start; justify-content: center; padding: 3rem 1rem; z-index: 50; overflow-y: auto; }
    .modal { background: var(--surface); border-radius: var(--radius-lg); width: min(780px, 100%); padding: 1.5rem;
      box-shadow: var(--shadow-2); }
    .modal-head { display: flex; justify-content: space-between; align-items: flex-start;
      h3 { margin: 0 0 0.4rem; .mono { font-family: var(--mono); } } }
    .close { border: none; background: none; font-size: 1.6rem; line-height: 1; cursor: pointer; color: var(--text-muted); }
    .close:hover { color: var(--text); }
    .modal h4 { margin: 1.2rem 0 0.5rem; font-size: 0.95rem; }
    .rationale { margin: 0.5rem 0 0; color: var(--text-muted); font-size: 0.85rem; font-style: italic; }
    .grp { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.4rem;
      li { display: flex; align-items: center; gap: 0.5rem; background: var(--surface-2); border-radius: var(--radius); padding: 0.5rem 0.7rem; }
      .t-title { flex: 1; }
      .muted { color: var(--text-muted); font-style: italic; }
      .x { border: none; background: var(--err-soft); color: var(--err); border-radius: 50%; width: 20px; height: 20px;
        cursor: pointer; font-weight: 700; }
      .x:hover { filter: brightness(0.95); }
    }
    .add-btn { margin-top: 0.5rem; border-style: dashed; }
    .picker { margin-top: 0.5rem; border: 1px solid var(--border); border-radius: var(--radius); max-height: 240px;
      overflow-y: auto; display: flex; flex-direction: column;
      .muted { color: var(--text-muted); font-style: italic; margin: 0; padding: 0.6rem 0.8rem; }
      .pick-row { display: flex; align-items: center; gap: 0.5rem; width: 100%; text-align: left;
        border: none; background: var(--surface); padding: 0.5rem 0.8rem; cursor: pointer; font-size: 0.85rem;
        font-family: inherit; border-bottom: 1px solid var(--border); }
      .pick-row:hover { background: var(--surface-alt); }
      .pick-row:last-child { border-bottom: none; }
      .t-title { flex: 1; }
      .badge.moved { background: var(--warn-soft); color: var(--warn); }
    }
    .repo-box { background: var(--surface-2); border-radius: var(--radius); padding: 0.7rem 0.9rem; margin-bottom: 0.5rem;
      .muted { color: var(--text-muted); font-style: italic; margin: 0; }
      .repo-current { display: flex; align-items: center; gap: 0.5rem; }
      .badge.repo { background: var(--ai-soft); color: var(--ai); }
      .badge.dev { background: var(--ok-soft, var(--surface)); color: var(--ok); }
      .warn { color: var(--warn); font-style: normal; font-size: 0.8rem; }
      .branch { color: var(--text-muted); font-size: 0.8rem; font-family: monospace; }
      .pr-states { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-top: 0.5rem; }
      .pr-chip { font-size: 0.78rem; padding: 0.15rem 0.5rem; border-radius: 999px; background: var(--surface); border: 1px solid var(--border);
        a { color: var(--ai); } }
      .pr-chip.pr-open { border-color: var(--ai); color: var(--ai); }
      .pr-chip.pr-merged { border-color: var(--ok); color: var(--ok); }
      .pr-chip.pr-conflict, .pr-chip.pr-error { border-color: var(--warn); color: var(--warn); }
      .repo-assign { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; margin-top: 0.6rem;
        select, input { padding: 0.35rem 0.5rem; border-radius: var(--radius); border: 1px solid var(--border); background: var(--surface); color: var(--text); }
        input { flex: 1; min-width: 140px; } }
    }
    .action-bar { display: flex; flex-wrap: wrap; gap: 0.5rem; }
    .review-panel { margin-top: 1rem; background: var(--surface-2); border-left: 4px solid var(--ok); border-radius: var(--radius); padding: 1rem;
      .summary { margin: 0 0 0.75rem; }
      .changes { list-style: none; margin: 0 0 1rem; padding: 0; display: flex; flex-direction: column; gap: 0.5rem; }
      .changes li { background: var(--surface); border-radius: var(--radius); padding: 0.5rem 0.7rem; font-size: 0.85rem; }
      .changes code { font-family: var(--mono); }
      .act { font-size: 0.7rem; font-weight: 700; padding: 0.1rem 0.45rem; border-radius: var(--radius-sm); margin-right: 0.4rem; text-transform: uppercase; }
      .act-create { background: var(--ok-soft); color: var(--ok); }
      .act-modify { background: var(--info-soft); color: var(--info); }
      .act-delete { background: var(--err-soft); color: var(--err); }
      .cd { color: var(--text-muted); margin-top: 0.25rem; }
      .review-actions { display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }
      .hint { color: var(--text-muted); font-size: 0.78rem; }
    }
    .whatif-result { margin-top: 1rem; border-radius: var(--radius); padding: 0.9rem 1.1rem; background: var(--surface-2);
      border-left: 4px solid var(--border-strong); font-size: 0.88rem;
      &.low { background: var(--ok-soft); border-left-color: var(--ok); }
      &.mid { background: var(--warn-soft); border-left-color: var(--warn); }
      &.high { background: var(--err-soft); border-left-color: var(--err); }
      .impact-row { display: flex; align-items: center; gap: 0.9rem; }
      .impact-pct { font-size: 1.6rem; font-weight: 700; }
      .meta { color: var(--text-muted); font-size: 0.8rem; }
      p { margin: 0.5rem 0 0; }
    }
    .terminal { margin-top: 1rem; border-radius: var(--radius); overflow: hidden; box-shadow: var(--shadow-2); }
    .term-bar { display: flex; align-items: center; gap: 0.6rem; background: #1e272e; padding: 0.5rem 0.9rem;
      .dots { display: flex; gap: 0.3rem;
        i { width: 11px; height: 11px; border-radius: 50%; background: #576574; display: block; }
        i:nth-child(1) { background: #ff5f56; } i:nth-child(2) { background: #ffbd2e; } i:nth-child(3) { background: #27c93f; } }
      .term-title { color: #c8d6e5; font-size: 0.78rem; font-family: var(--mono); }
      .live { margin-left: auto; color: #27c93f; font-size: 0.72rem; font-weight: 700; animation: blink 1.2s infinite; }
    }
    @keyframes blink { 50% { opacity: 0.35; } }
    .term-body { margin: 0; background: #0c0c0c; color: #dcdcdc; padding: 0.9rem 1rem; font-family: var(--mono);
      font-size: 0.8rem; line-height: 1.5; max-height: 320px; overflow-y: auto; white-space: pre-wrap;
      .ln { display: block; } .ln::before { content: '$ '; color: #27c93f; }
      .dim { color: #576574; &::before { content: ''; } } }
    .tests-panel { margin-top: 1rem; background: var(--surface-2); border-left: 4px solid var(--info);
      border-radius: var(--radius); padding: 1rem;
      .test-head { display: flex; align-items: center; gap: 0.75rem; flex-wrap: wrap;
        border-radius: var(--radius); padding: 0.6rem 0.9rem; background: var(--surface);
        &.ok { background: var(--ok-soft); .verdict { color: var(--ok); } }
        &.ko { background: var(--err-soft); .verdict { color: var(--err); } }
        &.warn { background: var(--warn-soft); .verdict { color: var(--warn); } }
        .verdict { font-weight: 700; font-size: 0.95rem; }
        .chips { display: flex; gap: 0.4rem; }
        .meta { margin-left: auto; color: var(--text-muted); font-size: 0.78rem;
          code { font-family: var(--mono); } }
        .live { color: var(--ok); font-weight: 700; margin-left: 0.5rem; animation: blink 1.2s infinite; } }
      .chip { font-size: 0.75rem; font-weight: 700; padding: 0.15rem 0.55rem; border-radius: 999px;
        &.ok { background: var(--ok-soft); color: var(--ok); }
        &.ko { background: var(--err-soft); color: var(--err); }
        &.sk { background: var(--surface-2); color: var(--text-muted); } }
      .test-note { margin: 0.6rem 0 0; font-size: 0.82rem; color: var(--text-muted); font-style: italic;
        &.err { color: var(--warn); font-style: normal; } }
      .test-group { margin-top: 1rem;
        h5 { margin: 0 0 0.5rem; font-size: 0.88rem; display: flex; align-items: center; gap: 0.5rem; }
        .tally { font-family: var(--mono); font-size: 0.78rem; color: var(--text-muted); font-weight: 600; }
        .muted { color: var(--text-muted); font-size: 0.85rem; margin: 0; font-style: italic;
          &.ok-text { color: var(--ok); font-style: normal; } } }
      .test-list { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 0.4rem;
        li { display: flex; gap: 0.6rem; background: var(--surface); border-radius: var(--radius);
          padding: 0.5rem 0.7rem; font-size: 0.85rem; border-left: 3px solid var(--ok); }
        li.failed { border-left-color: var(--err); background: var(--err-soft); }
        li.skipped { border-left-color: var(--border-strong); opacity: 0.7; }
        .st { font-weight: 700; }
        .st-passed { color: var(--ok); } .st-failed { color: var(--err); } .st-skipped { color: var(--text-muted); }
        .tc { flex: 1; min-width: 0; }
        .tname { font-weight: 600; }
        .tfile { color: var(--text-muted); font-size: 0.76rem; code { font-family: var(--mono); } }
        .tmsg { margin: 0.4rem 0 0; background: var(--sidebar); color: #dfe6e9; border-radius: var(--radius-sm);
          padding: 0.5rem 0.6rem; font-family: var(--mono); font-size: 0.72rem; white-space: pre-wrap;
          max-height: 160px; overflow-y: auto; } }
      > .muted { color: var(--text-muted); font-size: 0.85rem; margin: 0; font-style: italic; }
      .test-group > .tmsg { margin: 0.5rem 0 0; background: var(--sidebar); color: #dfe6e9;
        border-radius: var(--radius-sm); padding: 0.5rem 0.6rem; font-family: var(--mono);
        font-size: 0.72rem; white-space: pre-wrap; max-height: 200px; overflow-y: auto; } }
    .btn.tests { border-color: var(--info); color: var(--info); &:hover { background: var(--info-soft); } }
    .btn.tests-ghost { border-color: var(--border-strong); color: var(--text-muted); }
    .payload { margin-top: 1rem; background: var(--sidebar); color: #dfe6e9; border-radius: var(--radius); padding: 1rem;
      font-family: var(--mono); font-size: 0.78rem; overflow-x: auto; max-height: 360px; }
    .handoff-btn { border-color: var(--ai); color: var(--ai); &:hover { background: var(--ai-soft); } }
    .handoff-bar { display: flex; align-items: center; gap: 0.6rem; margin-top: 1rem; flex-wrap: wrap;
      font-size: 0.82rem; color: var(--text-muted); span { flex: 1; } }
  `,
})
export class WorkOrdersComponent implements OnInit, OnDestroy {
  private readonly api = inject(ApiService);
  private readonly auth = inject(AuthService);
  readonly projects = inject(ProjectService);

  readonly workorders = signal<WorkOrder[]>([]);
  readonly assignable = signal<EligibleTicket[]>([]);
  readonly loading = signal(false);
  readonly message = signal<{ text: string; error: boolean } | null>(null);

  readonly selected = signal<WorkOrder | null>(null);
  readonly panel = signal<Panel>(null);
  readonly showPicker = signal(false);
  readonly handoffText = signal<string>('');
  readonly analyzing = signal(false);
  readonly validating = signal(false);
  readonly impact = signal<WhatIfResult | null>(null);

  readonly canValidate = this.auth.hasRole('scrum_master');
  readonly canEdit = this.auth.hasRole('scrum_master');
  // L'esecuzione dell'agente di coding è un'azione dello Sviluppatore
  // (separazione dei compiti: lo Scrum Master valida, non esegue).
  readonly canExecute = this.auth.hasRole('developer');
  readonly executing = signal(false);
  readonly syncing = signal(false);

  // Vedere i test è un'esigenza condivisa: lo Sviluppatore li esegue mentre
  // lavora, lo Scrum Master li usa come evidenza prima del gate di validazione.
  readonly canRunTests = this.auth.hasRole('developer') || this.auth.hasRole('scrum_master');

  // gestione Git: repository configurati e assegnazione manuale al WO
  readonly repos = signal<RepoOption[]>([]);
  readonly assigningRepo = signal(false);
  repoChoice = '';
  baseBranch = '';

  // assegnazione dello sviluppatore (reviewer della PR)
  readonly developers = signal<Developer[]>([]);
  readonly assigningDev = signal(false);
  devChoice: number | null = null;

  // esecuzione dei test (Agente Tester)
  readonly testRun = signal<TestRun | null>(null);
  readonly testCases = signal<TestCaseResult[]>([]);
  readonly testLines = signal<string[]>([]);
  readonly testLive = signal(false);
  readonly startingTests = signal(false);
  // Valorizzato quando il backend rifiuta perché il branch di lavoro non esiste
  // ancora: abilita l'esecuzione della sola suite di regressione.
  readonly branchMissing = signal(false);
  private testPollHandle: ReturnType<typeof setInterval> | null = null;

  // terminale virtuale (tempo reale)
  readonly terminalLines = signal<string[]>([]);
  readonly terminalStatus = signal<string>('');
  readonly terminalLive = signal(false);
  private pollHandle: ReturnType<typeof setInterval> | null = null;
  private static readonly RUNNING_STATES = ['generated', 'running'];

  readonly myDomain = this.auth.user()?.domain ?? null;
  readonly onlyMyDomain = signal<boolean>(this.auth.hasRole('developer') && !!this.myDomain);

  readonly visible = computed(() =>
    this.onlyMyDomain() && this.myDomain
      ? this.workorders().filter((wo) => wo.tickets.some((t) => t.domain === this.myDomain))
      : this.workorders(),
  );

  ngOnInit(): void {
    this.projects.loadRepos();
    this.load();
    this.loadRepos();
    this.loadDevelopers();
  }

  changeProject(repoUrl: string): void {
    this.projects.setActive(repoUrl);
    this.load();
  }

  private loadRepos(): void {
    this.api.get<RepoOption[]>('/workorders/repos').subscribe({
      next: (repos) => this.repos.set(repos),
      error: () => this.repos.set([]),
    });
  }

  private loadDevelopers(): void {
    this.api.get<Developer[]>('/workorders/developers').subscribe({
      next: (devs) => this.developers.set(devs),
      error: () => this.developers.set([]),
    });
  }

  assignDeveloper(wo: WorkOrder): void {
    this.assigningDev.set(true);
    this.api
      .put<WorkOrder>(`/workorders/workorders/${wo.id}/developer`, { assignee_id: this.devChoice })
      .subscribe({
        next: (updated) => {
          this.assigningDev.set(false);
          this.selected.set(updated);
          this.workorders.update((list) => list.map((w) => (w.id === updated.id ? updated : w)));
          this.notify(
            updated.assignee_username
              ? `Sviluppatore assegnato: ${updated.assignee_username}`
              : 'Assegnazione rimossa',
          );
        },
        error: (err) => {
          this.assigningDev.set(false);
          this.notify(err.error?.detail ?? "Errore nell'assegnazione dello sviluppatore", true);
        },
      });
  }

  /** Il repo si può ancora assegnare finché il codice non è validato/unito. */
  canAssignRepo(wo: WorkOrder): boolean {
    return this.canEdit && wo.status !== 'validated' && wo.status !== 'merged';
  }

  repoName(url: string | null): string {
    if (!url) return '';
    return this.repos().find((r) => r.url === url)?.name ?? url;
  }

  assignRepo(wo: WorkOrder): void {
    if (!this.repoChoice) {
      this.notify('Seleziona un repository', true);
      return;
    }
    this.assigningRepo.set(true);
    this.api
      .put<WorkOrder>(`/workorders/workorders/${wo.id}/repo`, {
        repo_url: this.repoChoice,
        base_branch: this.baseBranch.trim(),
      })
      .subscribe({
        next: (updated) => {
          this.assigningRepo.set(false);
          this.selected.set(updated);
          this.workorders.update((list) => list.map((w) => (w.id === updated.id ? updated : w)));
          this.notify(`Repository assegnato: ${this.repoName(updated.repo_url)}`);
        },
        error: (err) => {
          this.assigningRepo.set(false);
          this.notify(err.error?.detail ?? "Errore nell'assegnazione del repository", true);
        },
      });
  }

  ngOnDestroy(): void {
    this.stopPolling();
    this.stopTestPolling();
  }

  statusLabel(status: string): string {
    return WO_STATUS_LABELS[status] ?? status;
  }

  prLabel(state: string): string {
    return (
      { none: 'nessuna', draft: 'bozza', open: 'aperta', merged: 'unita', closed: 'chiusa',
        conflict: 'conflitti', error: 'errore' }[state] ??
      state
    );
  }

  ticketsPreview(wo: WorkOrder): string {
    const titles = wo.tickets.map((t) => t.title ?? '').filter(Boolean);
    const head = titles.slice(0, 2).join(', ');
    return titles.length > 2 ? `${head} +${titles.length - 2}` : head || '(nessun ticket)';
  }

  isEditable(wo: WorkOrder): boolean {
    return wo.status === 'generated' || wo.status === 'failed';
  }

  open(wo: WorkOrder): void {
    this.stopPolling();
    this.stopTestPolling();
    this.panel.set(null);
    this.impact.set(null);
    this.showPicker.set(false);
    this.resetTests();
    this.selected.set(wo);
    this.repoChoice = wo.repo_url ?? '';
    this.baseBranch = wo.base_branch ?? '';
    if (this.canEdit && this.isEditable(wo)) {
      this.loadAssignable(wo.id);
    }
    // se la PR è aperta, allinea lo stato con GitHub (potrebbe essere stata fusa/chiusa a mano)
    if (wo.code_pr_state === 'open') {
      this.syncPr(wo, true);
    }
  }

  close(): void {
    this.stopPolling();
    this.stopTestPolling();
    this.selected.set(null);
    this.panel.set(null);
    this.showPicker.set(false);
  }

  togglePicker(): void {
    this.showPicker.update((v) => !v);
  }

  showHandoff(wo: WorkOrder): void {
    if (this.panel() === 'handoff') {
      this.panel.set(null);
      return;
    }
    this.handoffText.set('Caricamento del JSON di handoff…');
    this.panel.set('handoff');
    this.api.get<unknown>(`/workorders/workorders/${wo.id}/handoff`).subscribe({
      next: (h) => this.handoffText.set(JSON.stringify(h, null, 2)),
      error: (err) => this.handoffText.set('Errore: ' + (err.error?.detail ?? 'JSON non disponibile')),
    });
  }

  copyHandoff(): void {
    navigator.clipboard?.writeText(this.handoffText()).then(
      () => this.notify('JSON copiato negli appunti'),
      () => this.notify('Copia non riuscita', true),
    );
  }

  downloadHandoff(wo: WorkOrder): void {
    const blob = new Blob([this.handoffText()], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `handoff-${wo.id.slice(0, 8)}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  private notify(text: string, error = false): void {
    this.message.set({ text, error });
    setTimeout(() => this.message.set(null), 7000);
  }

  executeWo(wo: WorkOrder, force = false): void {
    if (force && !confirm('Rigenerare il Work Order? Le modifiche precedenti verranno scartate.')) {
      return;
    }
    this.executing.set(true);
    this.api.post<{ detail?: string }>('/coding/execute', { wo_id: wo.id, force }).subscribe({
      next: (res) => {
        this.executing.set(false);
        this.notify(res.detail ?? 'Elaborazione avviata');
        this.panel.set(null);
        this.toggleTerminal(wo); // apre il terminale del reasoning in tempo reale
        this.load();
      },
      error: (err) => {
        this.executing.set(false);
        this.notify(err.error?.detail ?? "Errore nell'avvio dell'elaborazione", true);
      },
    });
  }

  /** Motivo per cui l'esecuzione è bloccata (o null se si può procedere). */
  execBlocked(wo: WorkOrder): string | null {
    if (!wo.repo_url) return 'Manca il repository: assegnazione a cura dello Scrum Master';
    if (!wo.assignee_id) return 'Manca lo sviluppatore: assegnazione a cura dello Scrum Master';
    return null;
  }

  /** Allinea lo stato del WO con la PR reale su GitHub (merge/chiusura fatti a mano). */
  syncPr(wo: WorkOrder, silent = false): void {
    this.syncing.set(true);
    this.api.post<{ synced?: boolean; code_pr_state?: string; wo_status?: string }>(
      `/ingestion/workorders/${wo.id}/sync-pr`, {},
    ).subscribe({
      next: (res) => {
        this.syncing.set(false);
        if (res.synced) {
          this.load();
          const msg = res.code_pr_state === 'merged'
            ? 'PR fusa su GitHub: Work Order validato'
            : res.code_pr_state === 'closed'
              ? 'PR chiusa su GitHub: Work Order rifiutato'
              : 'Stato PR aggiornato';
          this.notify(msg);
        } else if (!silent) {
          this.notify('La PR è ancora aperta su GitHub');
        }
      },
      error: (err) => {
        this.syncing.set(false);
        if (!silent) this.notify(err.error?.detail ?? 'Errore nel sincronizzare la PR', true);
      },
    });
  }

  // ---- terminale ----
  private stopPolling(): void {
    if (this.pollHandle !== null) {
      clearInterval(this.pollHandle);
      this.pollHandle = null;
    }
    this.terminalLive.set(false);
  }

  toggleTerminal(wo: WorkOrder): void {
    if (this.panel() === 'terminal') {
      this.stopPolling();
      this.panel.set(null);
      return;
    }
    // I pannelli sono mutuamente esclusivi: fermo l'altro poller.
    this.stopTestPolling();
    this.panel.set('terminal');
    this.terminalLines.set([]);
    this.terminalStatus.set(wo.status);
    this.fetchLog(wo.id);
    this.pollHandle = setInterval(() => this.fetchLog(wo.id), 1500);
  }

  private fetchLog(woId: string): void {
    this.api.get<ExecutionLog>(`/coding/executions/${woId}/log`).subscribe({
      next: (log) => {
        if (this.panel() !== 'terminal' || this.selected()?.id !== woId) {
          return;
        }
        this.terminalLines.set(log.lines);
        this.terminalStatus.set(log.status);
        const running = WorkOrdersComponent.RUNNING_STATES.includes(log.status);
        this.terminalLive.set(running);
        if (!running) {
          this.stopPolling();
          // elaborazione finita: ricarico il WO per riflettere code_pr_state e,
          // dopo qualche secondo, di nuovo per cogliere la PR aperta dalla callback.
          this.load();
          setTimeout(() => this.load(), 3500);
          setTimeout(() => this.load(), 8000);
        }
      },
      error: () => this.stopPolling(),
    });
  }

  // ---- test (Agente Tester) ----
  private stopTestPolling(): void {
    if (this.testPollHandle !== null) {
      clearInterval(this.testPollHandle);
      this.testPollHandle = null;
    }
    this.testLive.set(false);
  }

  private resetTests(): void {
    this.testRun.set(null);
    this.testCases.set([]);
    this.testLines.set([]);
    this.branchMissing.set(false);
  }

  /** Motivo per cui i test non si possono eseguire, o null se si può procedere. */
  testsBlocked(wo: WorkOrder): string | null {
    if (!wo.repo_url) return 'Manca il repository: assegnazione a cura dello Scrum Master';
    if (wo.status === 'generated' || wo.status === 'running') {
      return "Il codice non è ancora stato prodotto dall'Agente Sviluppatore";
    }
    return null;
  }

  toggleTests(wo: WorkOrder): void {
    if (this.panel() === 'tests') {
      this.stopTestPolling();
      this.panel.set(null);
      return;
    }
    // I pannelli sono mutuamente esclusivi: fermo il terminale del reasoning.
    this.stopPolling();
    this.panel.set('tests');
    this.fetchTests(wo.id);
    // Intervallo più lungo del terminale del reasoning: le risposte contengono
    // anche i singoli casi, quindi sono più pesanti.
    this.testPollHandle = setInterval(() => this.fetchTests(wo.id), 2000);
  }

  private fetchTests(woId: string): void {
    this.api.get<TestRunLatest>(`/testing/workorders/${woId}/test-runs/latest?with_log=true`).subscribe({
      next: (res) => {
        if (this.panel() !== 'tests' || this.selected()?.id !== woId) {
          return;
        }
        this.testRun.set(res.run);
        this.testCases.set(res.cases ?? []);
        this.testLines.set(res.lines ?? []);
        const running = res.run?.status === 'running';
        this.testLive.set(running);
        if (!running) {
          this.stopTestPolling();
        }
      },
      error: () => this.stopTestPolling(),
    });
  }

  runTests(wo: WorkOrder, allowBaseBranch = false): void {
    this.startingTests.set(true);
    this.api
      .post<{ detail?: string }>('/testing/test-runs', {
        wo_id: wo.id,
        allow_base_branch: allowBaseBranch,
      })
      .subscribe({
        next: (res) => {
          this.startingTests.set(false);
          this.branchMissing.set(false);
          this.notify(res.detail ?? 'Esecuzione dei test avviata');
          this.resetTests();
          if (this.panel() !== 'tests') {
            this.toggleTests(wo);
          }
        },
        error: (err) => {
          this.startingTests.set(false);
          const detail = err.error?.detail ?? "Errore nell'avvio dei test";
          // Il branch di lavoro non esiste ancora: si offre la sola regressione
          // invece di lasciare l'utente davanti a un errore senza vie d'uscita.
          this.branchMissing.set(String(detail).includes('non esiste ancora'));
          this.notify(detail, true);
        },
      });
  }

  casesFor(origin: 'new' | 'regression'): TestCaseResult[] {
    const cases = this.testCases().filter((c) => c.origin === origin);
    // Della regressione interessano solo i falliti: sono centinaia, e la domanda
    // a cui si risponde è se l'intervento abbia rotto qualcosa.
    return origin === 'regression' ? cases.filter((c) => c.status === 'failed') : cases;
  }

  /** Sintesi delle due verifiche di compilazione, per l'intestazione del blocco. */
  compilazioneEsito(r: TestRun): string {
    const esiti = [r.typecheck_status, r.build_status].filter((s) => s !== 'skipped');
    if (esiti.some((s) => s === 'failed')) return 'non superata';
    return esiti.length ? 'superata' : 'non eseguita';
  }

  statusIcon(status: string): string {
    return ({ passed: '✓', failed: '✗', skipped: '–' } as Record<string, string>)[status] ?? '?';
  }

  testVerdict(r: TestRun): string {
    // Un codice che non compila è la notizia più importante, e precede il conteggio.
    if (r.status !== 'running') {
      if (r.typecheck_status === 'failed') return '✗ Il codice consegnato non compila';
      if (r.build_status === 'failed') return '✗ Il codice consegnato non è impacchettabile';
    }
    return (
      {
        running: 'Esecuzione dei test in corso…',
        passed: '✓ Tutti i test sono passati',
        failed: `✗ ${r.failed_tests} test non superati`,
        no_tests: '⚠ Nessun test eseguito',
        error: '⚠ Esecuzione dei test non riuscita',
      } as Record<string, string>
    )[r.status] ?? r.status;
  }

  testStageLabel(stage: string | null): string {
    return (
      {
        workspace: 'la preparazione del codice',
        install: "l'installazione delle dipendenze",
        db_unavailable: 'la connessione al database di test',
        db_schema: 'la creazione dello schema del database',
        vitest: "l'avvio della suite",
        timeout: "l'esecuzione (tempo massimo superato)",
        report: 'la lettura del report',
      } as Record<string, string>
    )[stage ?? ''] ?? 'una fase non identificata';
  }

  // ---- dati ----
  load(): void {
    this.loading.set(true);
    const repo = this.projects.activeRepo();
    const q = repo ? `?repo_url=${encodeURIComponent(repo)}` : '';
    this.api.get<WorkOrder[]>(`/workorders/workorders${q}`).subscribe({
      next: (rows) => {
        this.workorders.set(rows);
        this.loading.set(false);
        // se un WO è aperto, aggiornalo con i dati freschi
        const sel = this.selected();
        if (sel) {
          const fresh = rows.find((w) => w.id === sel.id) ?? null;
          this.selected.set(fresh);
          if (fresh && this.canEdit && this.isEditable(fresh)) {
            this.loadAssignable(fresh.id);
          }
        }
      },
      error: () => this.loading.set(false),
    });
  }

  loadAssignable(woId: string): void {
    this.api.get<EligibleTicket[]>(`/workorders/workorders/${woId}/assignable`).subscribe({
      next: (rows) => this.assignable.set(rows),
      error: () => this.assignable.set([]),
    });
  }

  // ---- azioni ----
  asChanges(changes: unknown): { summary: string; changes: CodeChange[] } | null {
    if (changes && typeof changes === 'object' && 'changes' in changes) {
      const c = changes as { summary?: string; changes?: CodeChange[] };
      return { summary: c.summary ?? '', changes: c.changes ?? [] };
    }
    return null;
  }

  validate(wo: WorkOrder, approve: boolean): void {
    this.validating.set(true);
    this.api.post<{ detail: string }>(`/workorders/workorders/${wo.id}/validate`, { approve, note: '' }).subscribe({
      next: (res) => {
        this.validating.set(false);
        this.panel.set(null);
        this.notify(res.detail);
        this.load();
      },
      error: (err) => {
        this.validating.set(false);
        this.notify(err.error?.detail ?? 'Errore nella validazione del codice', true);
      },
    });
  }

  addTicket(wo: WorkOrder, ticketId: string): void {
    if (!ticketId) {
      return;
    }
    const candidate = this.assignable().find((e) => e.id === ticketId);
    let move = false;
    if (candidate?.current_wo_id) {
      const ok = window.confirm(
        `"${candidate.title}" è già nel Work Order ${candidate.current_wo_id.slice(0, 8)}.\n` +
          'Vuoi spostarlo in questo Work Order? Verrà rimosso dall\'altro.',
      );
      if (!ok) {
        return;
      }
      move = true;
    }
    this.api.post<{ detail?: string }>(`/workorders/workorders/${wo.id}/tickets`, { ticket_id: ticketId, move }).subscribe({
      next: (res) => {
        this.notify(res.detail ?? 'Ticket aggiunto al Work Order');
        this.showPicker.set(false);
        this.load();
      },
      error: (err) => this.notify(err.error?.detail ?? "Errore nell'aggiunta del ticket", true),
    });
  }

  removeTicket(wo: WorkOrder, ticketId: string): void {
    this.api.delete(`/workorders/workorders/${wo.id}/tickets/${ticketId}`).subscribe({
      next: () => {
        this.notify('Ticket rimosso dal Work Order');
        this.load();
      },
      error: (err) => this.notify(err.error?.detail ?? 'Errore nella rimozione del ticket', true),
    });
  }

  deleteWo(wo: WorkOrder): void {
    if (!window.confirm(`Eliminare il Work Order ${wo.id.slice(0, 8)}? I ticket torneranno disponibili.`)) {
      return;
    }
    this.api.delete(`/workorders/workorders/${wo.id}`).subscribe({
      next: () => {
        this.notify('Work Order eliminato');
        this.close();
        this.load();
      },
      error: (err) => this.notify(err.error?.detail ?? "Errore nell'eliminazione del Work Order", true),
    });
  }

  decisionLabel(decision: string): string {
    return (
      {
        auto_approved: 'Impatto basso: procedura rapida (<5%)',
        needs_authorization: 'Impatto medio: serve l’autorizzazione dello Scrum Master (5–30%)',
        blocked: 'Impatto alto: da posticipare con Report degli Scarti (>30%)',
      }[decision] ?? decision
    );
  }

  runWhatIf(wo: WorkOrder): void {
    const tickets = (wo.payload['tickets'] ?? []) as { title?: string; description?: string }[];
    const description = tickets
      .map((t) => [t.title, t.description].filter(Boolean).join(': '))
      .join('\n')
      .trim();
    if (description.length < 10) {
      this.notify('Ticket senza descrizione sufficiente per il What-If', true);
      return;
    }
    this.analyzing.set(true);
    this.api.post<WhatIfResult>('/impact/what-if', { description }).subscribe({
      next: (result) => {
        this.analyzing.set(false);
        this.impact.set(result);
        this.panel.set('whatif');
      },
      error: (err) => {
        this.analyzing.set(false);
        this.notify(err.error?.detail ?? "Analisi d'impatto non disponibile", true);
      },
    });
  }
}
