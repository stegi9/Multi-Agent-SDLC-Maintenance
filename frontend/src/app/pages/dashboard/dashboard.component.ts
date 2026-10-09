import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { ApiService } from '../../core/services/api.service';
import { Ticket, WorkOrder } from '../../core/models';

@Component({
  selector: 'app-dashboard',
  standalone: true,
  imports: [RouterLink],
  template: `
    <h2 class="page-title">System Overview</h2>
    <p class="desc">
      Metriche e stato operativo della piattaforma: ticket, pipeline e Work Order.
    </p>
    <div class="cards">
      <a class="card" routerLink="/tickets">
        <div class="card-top">
          <span class="card-label">Ticket totali</span>
          <span class="card-ic mi">confirmation_number</span>
        </div>
        <span class="value">{{ tickets().length }}</span>
      </a>
      <a class="card" routerLink="/tickets">
        <div class="card-top">
          <span class="card-label">Da valutare</span>
          <span class="card-ic warn mi">warning</span>
        </div>
        <span class="value">{{ rawCount() }} <small class="warn-text">grezzi</small></span>
      </a>
      <a class="card" routerLink="/work-orders">
        <div class="card-top">
          <span class="card-label">In lavorazione</span>
          <span class="card-ic info mi">sync</span>
        </div>
        <span class="value">{{ inProgressCount() }} <small>attivi</small></span>
      </a>
      <a class="card" routerLink="/work-orders">
        <div class="card-top">
          <span class="card-label">Work Order</span>
          <span class="card-ic ok mi">deployed_code</span>
        </div>
        <span class="value">{{ workorders().length }}</span>
      </a>
    </div>

    <h3 class="section">Sorgenti collegate</h3>
    <div class="sources">
      @for (s of sources(); track s.name) {
        <div class="source" [class.on]="s.configured">
          <span class="src-name">{{ s.name }}</span>
          <span class="src-state" [class.on]="s.configured">
            {{ s.configured ? '● connesso' : '○ non configurato' }}
          </span>
        </div>
      }
    </div>
  `,
  styles: `
    .page-title { margin: 0 0 0.25rem; font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; }
    .desc { color: var(--text-muted); font-size: 0.9rem; margin-top: 0; }
    .cards {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
      margin-top: 1.5rem;
    }
    .card {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.25rem;
      box-shadow: var(--shadow-1);
      text-decoration: none;
      color: inherit;
      display: flex;
      flex-direction: column;
      gap: 0.6rem;

      &:hover { border-color: var(--border-strong); }
      .card-top { display: flex; align-items: center; justify-content: space-between; }
      .card-label { font-size: 0.72rem; font-weight: 600; color: var(--text-muted);
        text-transform: uppercase; letter-spacing: 0.06em; }
      .card-ic { font-size: 22px; color: var(--text-muted); background: var(--surface-2);
        border-radius: var(--radius); width: 38px; height: 38px; display: flex; align-items: center; justify-content: center;
        &.ok { color: var(--ok); background: var(--ok-soft); }
        &.warn { color: var(--warn); background: var(--warn-soft); }
        &.info { color: var(--info); background: var(--info-soft); }
      }
      .value { font-size: 2rem; font-weight: 700; letter-spacing: -0.02em;
        small { font-size: 0.8rem; font-weight: 500; color: var(--text-muted); }
        .warn-text { color: var(--warn); }
      }
    }
    .section { margin-top: 2rem; font-size: 1.05rem; }
    .sources { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 1rem; }
    .source {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1rem 1.1rem;
      box-shadow: var(--shadow-1);
      display: flex; align-items: center; justify-content: space-between;
      border-left: 3px solid var(--border-strong);

      &.on { border-left-color: var(--ok); }
      .src-name { font-weight: 600; text-transform: capitalize; }
      .src-state { font-size: 0.78rem; font-weight: 600; color: var(--text-muted);
        &.on { color: var(--ok); } }
    }
  `,
})
export class DashboardComponent implements OnInit {
  private readonly api = inject(ApiService);

  readonly tickets = signal<Ticket[]>([]);
  readonly workorders = signal<WorkOrder[]>([]);
  readonly sources = signal<{ name: string; configured: boolean }[]>([]);

  readonly rawCount = computed(() => this.tickets().filter((t) => t.status === 'raw').length);
  readonly inProgressCount = computed(
    () => this.tickets().filter((t) => t.status === 'in_progress').length,
  );

  ngOnInit(): void {
    this.api.get<Ticket[]>('/ingestion/tickets').subscribe({
      next: (rows) => this.tickets.set(rows),
      error: () => undefined,
    });
    this.api.get<WorkOrder[]>('/workorders/workorders').subscribe({
      next: (rows) => this.workorders.set(rows),
      error: () => undefined,
    });
    this.api.get<{ name: string; configured: boolean }[]>('/ingestion/sources').subscribe({
      next: (rows) => this.sources.set(rows),
      error: () => undefined,
    });
  }
}
