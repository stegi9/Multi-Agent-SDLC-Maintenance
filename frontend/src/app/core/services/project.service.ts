import { Injectable, computed, inject, signal } from '@angular/core';
import { ApiService } from './api.service';
import { RepoOption } from '../models';

const STORAGE_KEY = 'active-project-repo';

/**
 * Progetto attivo della dashboard. Un progetto = un repository Git.
 * Lo Scrum Master può avere più repository/progetti: il progetto selezionato
 * filtra ticket e Work Order, così i progetti restano separati.
 */
@Injectable({ providedIn: 'root' })
export class ProjectService {
  private readonly api = inject(ApiService);

  readonly repos = signal<RepoOption[]>([]);
  readonly activeRepo = signal<string>(localStorage.getItem(STORAGE_KEY) ?? '');

  /** true quando esistono progetti configurati tra cui scegliere. */
  readonly hasProjects = computed(() => this.repos().length > 0);

  loadRepos(): void {
    this.api.get<RepoOption[]>('/workorders/repos').subscribe({
      next: (repos) => {
        this.repos.set(repos);
        // se il progetto attivo non è più configurato, ripiega sul primo
        const active = this.activeRepo();
        if (repos.length && !repos.some((r) => r.url === active)) {
          this.setActive(repos[0].url);
        }
      },
      error: () => this.repos.set([]),
    });
  }

  setActive(repoUrl: string): void {
    this.activeRepo.set(repoUrl);
    if (repoUrl) {
      localStorage.setItem(STORAGE_KEY, repoUrl);
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  }

  name(url: string): string {
    return this.repos().find((r) => r.url === url)?.name ?? url;
  }
}
