import { Component, inject, signal } from '@angular/core';
import { RouterLink, RouterLinkActive, RouterOutlet } from '@angular/router';
import { AuthService } from './core/services/auth.service';
import { ROLE_LABELS } from './core/models';

@Component({
  selector: 'app-root',
  standalone: true,
  imports: [RouterOutlet, RouterLink, RouterLinkActive],
  template: `
    @if (auth.user(); as user) {
      <div class="layout">
        <aside class="sidebar">
          <div class="brand">
            <span class="brand-icon mi">terminal</span>
            <div>
              <h1>AgenticSDLC</h1>
              <p>AI Operations</p>
            </div>
          </div>

          <nav class="nav">
            <a routerLink="/dashboard" routerLinkActive="active">
              <span class="mi">dashboard</span> Dashboard
            </a>
            <a routerLink="/tickets" routerLinkActive="active">
              <span class="mi">confirmation_number</span> Ticket
            </a>
            <a routerLink="/work-orders" routerLinkActive="active">
              <span class="mi">deployed_code</span> Work Order
            </a>
            <a routerLink="/documents" routerLinkActive="active">
              <span class="mi">description</span> Documenti
            </a>
            @if (auth.hasRole('scrum_master')) {
              <a routerLink="/settings" routerLinkActive="active">
                <span class="mi">settings</span> Impostazioni
              </a>
            }
          </nav>

          <div class="side-footer">
            <button class="me" (click)="menuOpen.set(!menuOpen())">
              <span class="avatar">{{ initials(user.username) }}</span>
              <span class="me-info">
                <strong>{{ user.username }}</strong>
                <small>{{ primaryRole(user.roles) }}</small>
              </span>
              <span class="mi chev">expand_less</span>
            </button>
            @if (menuOpen()) {
              <div class="menu">
                <div class="who">
                  <strong>{{ user.username }}</strong>
                  <small>{{ user.email }}</small>
                </div>
                <div class="roles">
                  @for (role of user.roles; track role) {
                    <span class="chip">{{ roleLabel(role) }}</span>
                  }
                  @if (user.domain) {
                    <span class="chip domain">dominio: {{ user.domain }}</span>
                  }
                </div>
                <button class="logout" (click)="logout()">
                  <span class="mi">logout</span> Esci
                </button>
              </div>
            }
          </div>
        </aside>

        <div class="main">
          <header class="topbar">
            <div class="crumb">
              <span class="mi">deployed_code</span>
              <span>AgenticSDLC — AI Operations</span>
            </div>
            <span class="spacer"></span>
            <span class="env-chip">{{ roleChip(user.roles) }}</span>
          </header>
          <main class="content" (click)="menuOpen.set(false)">
            <router-outlet />
          </main>
        </div>
      </div>
    } @else {
      <router-outlet />
    }
  `,
  styles: `
    .layout { display: flex; min-height: 100vh; }

    .sidebar {
      width: 260px;
      flex-shrink: 0;
      background: var(--sidebar);
      color: var(--sidebar-text);
      display: flex;
      flex-direction: column;
      padding: 1.25rem 0.85rem;
      gap: 1.5rem;
      position: sticky;
      top: 0;
      height: 100vh;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 0.7rem;
      padding: 0.25rem 0.5rem 1.1rem;
      border-bottom: 1px solid rgb(255 255 255 / 8%);

      .brand-icon {
        width: 36px; height: 36px;
        display: flex; align-items: center; justify-content: center;
        background: var(--ai); color: #fff; border-radius: var(--radius);
        font-variation-settings: 'FILL' 1;
      }
      h1 { margin: 0; font-size: 1.05rem; color: #fff; font-weight: 700; letter-spacing: -0.01em; }
      p { margin: 0; font-size: 0.7rem; color: var(--sidebar-text); text-transform: uppercase; letter-spacing: 0.08em; }
    }
    .nav {
      display: flex;
      flex-direction: column;
      gap: 0.25rem;

      a {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        color: var(--sidebar-text);
        text-decoration: none;
        padding: 0.6rem 0.75rem;
        border-radius: var(--radius);
        font-size: 0.9rem;
        font-weight: 500;

        .mi { font-size: 20px; }
        &:hover { background: rgb(255 255 255 / 6%); color: var(--sidebar-text-strong); }
        &.active { background: var(--sidebar-active); color: #fff; }
      }
    }
    .side-footer { margin-top: auto; position: relative; }
    .me {
      width: 100%;
      display: flex; align-items: center; gap: 0.6rem;
      background: rgb(255 255 255 / 5%);
      border: 1px solid rgb(255 255 255 / 8%);
      border-radius: var(--radius);
      padding: 0.5rem 0.6rem;
      cursor: pointer;
      color: var(--sidebar-text-strong);

      &:hover { background: rgb(255 255 255 / 9%); }
      .me-info { display: flex; flex-direction: column; text-align: left; flex: 1; overflow: hidden;
        strong { font-size: 0.82rem; color: #fff; }
        small { font-size: 0.7rem; color: var(--sidebar-text); }
      }
      .chev { font-size: 18px; }
    }
    .avatar {
      width: 32px; height: 32px; flex-shrink: 0;
      border-radius: 50%;
      background: var(--ai); color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-weight: 700; font-size: 0.8rem; text-transform: uppercase;
    }
    .menu {
      position: absolute;
      left: 0; right: 0; bottom: calc(100% + 0.5rem);
      background: var(--surface);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      box-shadow: var(--shadow-2);
      padding: 1rem;
      display: flex; flex-direction: column; gap: 0.75rem;
      z-index: 20;

      .who { display: flex; flex-direction: column; small { color: var(--text-muted); } }
      .roles { display: flex; flex-wrap: wrap; gap: 0.35rem; }
      .chip {
        background: var(--ai-soft); color: var(--ai);
        border-radius: 999px; padding: 0.15rem 0.6rem; font-size: 0.72rem; font-weight: 600;
        &.domain { background: var(--ok-soft); color: var(--ok); }
      }
      .logout {
        display: flex; align-items: center; justify-content: center; gap: 0.4rem;
        border: none; border-radius: var(--radius);
        background: var(--err-soft); color: var(--err);
        padding: 0.55rem; font-weight: 600; cursor: pointer; font-family: inherit;
        .mi { font-size: 18px; }
        &:hover { filter: brightness(0.97); }
      }
    }

    .main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
    .topbar {
      display: flex;
      align-items: center;
      gap: 0.75rem;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      padding: 0.6rem 1.5rem;
      min-height: 64px;
      position: sticky; top: 0; z-index: 10;
    }
    .crumb {
      display: flex; align-items: center; gap: 0.5rem;
      color: var(--text-muted); font-size: 0.9rem; font-weight: 500;
      .mi { font-size: 20px; color: var(--ai); }
    }
    .spacer { flex: 1; }
    .env-chip {
      background: var(--ai-soft); color: var(--ai);
      border-radius: 999px; padding: 0.25rem 0.7rem;
      font-size: 0.75rem; font-weight: 600;
    }
    .content { flex: 1; padding: 1.75rem 2rem; }
  `,
})
export class AppComponent {
  readonly auth = inject(AuthService);
  readonly menuOpen = signal(false);

  initials(username: string): string {
    return username.slice(0, 2);
  }

  roleLabel(role: string): string {
    return ROLE_LABELS[role] ?? role;
  }

  primaryRole(roles: string[]): string {
    return roles.length ? this.roleLabel(roles[0]) : '';
  }

  roleChip(roles: string[]): string {
    return roles.length ? this.roleLabel(roles[0]) : 'Utente';
  }

  logout(): void {
    this.menuOpen.set(false);
    this.auth.logout();
  }
}
