import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { Router } from '@angular/router';
import { AuthService } from '../../core/services/auth.service';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="login-wrapper">
      <form class="login-card" (ngSubmit)="submit()">
        <div class="brand">
          <span class="brand-icon mi">terminal</span>
          <div>
            <h1>AgenticSDLC</h1>
            <p class="ops">AI Operations</p>
          </div>
        </div>
        <p class="subtitle">Automazione del ciclo di vita del software con agenti AI</p>

        <label for="username">Username</label>
        <input id="username" name="username" [(ngModel)]="username" autocomplete="username" required />

        <label for="password">Password</label>
        <input id="password" name="password" type="password" [(ngModel)]="password"
               autocomplete="current-password" required />

        @if (error()) {
          <div class="error">{{ error() }}</div>
        }

        <button type="submit" [disabled]="loading()">
          {{ loading() ? 'Accesso in corso…' : 'Accedi' }}
        </button>

        <p class="hint">
          Utenti demo: <code>pm</code>, <code>sm</code>, <code>dev</code> — password
          <code>password123</code>
        </p>
      </form>
    </div>
  `,
  styles: `
    .login-wrapper {
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
      font-family: var(--font);
    }
    .login-card {
      background: var(--surface);
      border-radius: var(--radius-lg);
      padding: 2.5rem;
      width: 380px;
      box-shadow: var(--shadow-2);
      display: flex;
      flex-direction: column;
      gap: 0.5rem;

      .brand { display: flex; align-items: center; gap: 0.7rem; margin-bottom: 0.25rem;
        .brand-icon { width: 40px; height: 40px; display: flex; align-items: center; justify-content: center;
          background: var(--ai); color: #fff; border-radius: var(--radius); font-variation-settings: 'FILL' 1; }
        h1 { margin: 0; font-size: 1.25rem; font-weight: 700; letter-spacing: -0.01em; }
        .ops { margin: 0; font-size: 0.7rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.08em; }
      }
      .subtitle { margin: 0 0 1rem; color: var(--text-muted); font-size: 0.85rem; }
      label { font-size: 0.8rem; font-weight: 600; color: var(--text); }
      input {
        padding: 0.6rem 0.75rem;
        border: 1px solid var(--border);
        border-radius: var(--radius);
        font-size: 0.95rem;
        margin-bottom: 0.5rem;
        font-family: inherit;
        &:focus { outline: none; border-color: var(--ai); box-shadow: 0 0 0 3px var(--ai-soft); }
      }
      button[type=submit] {
        margin-top: 0.5rem;
        padding: 0.7rem;
        border: none;
        border-radius: var(--radius);
        background: var(--primary);
        color: var(--primary-contrast);
        font-size: 0.95rem;
        font-weight: 600;
        cursor: pointer;
        font-family: inherit;

        &:hover { background: var(--primary-hover); }
        &:disabled { opacity: 0.6; cursor: default; }
      }
      .error {
        background: var(--err-soft);
        color: var(--err);
        border-radius: var(--radius);
        padding: 0.6rem 0.75rem;
        font-size: 0.85rem;
      }
      .hint { margin: 1rem 0 0; font-size: 0.75rem; color: var(--text-muted); text-align: center;
        code { font-family: var(--mono); background: var(--surface-2); padding: 0.05rem 0.3rem; border-radius: var(--radius-sm); } }
    }
  `,
})
export class LoginComponent {
  private readonly auth = inject(AuthService);
  private readonly router = inject(Router);

  username = '';
  password = '';
  readonly loading = signal(false);
  readonly error = signal<string | null>(null);

  submit(): void {
    if (!this.username || !this.password) {
      this.error.set('Inserisci username e password');
      return;
    }
    this.loading.set(true);
    this.error.set(null);
    this.auth.login(this.username, this.password).subscribe({
      next: () => this.router.navigate(['/dashboard']),
      error: (err) => {
        this.loading.set(false);
        this.error.set(err.status === 401 ? 'Credenziali non valide' : 'Servizio di autenticazione non raggiungibile');
      },
    });
  }
}
