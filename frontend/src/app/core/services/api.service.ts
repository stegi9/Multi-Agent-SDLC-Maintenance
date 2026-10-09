import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';

/**
 * Client HTTP verso i microservizi.
 * I path /api/<servizio>/... sono instradati da nginx (produzione)
 * o dal proxy di sviluppo (proxy.conf.json).
 */
@Injectable({ providedIn: 'root' })
export class ApiService {
  private readonly http = inject(HttpClient);

  get<T>(path: string): Observable<T> {
    return this.http.get<T>(`/api${path}`);
  }

  post<T>(path: string, body: unknown): Observable<T> {
    return this.http.post<T>(`/api${path}`, body);
  }

  put<T>(path: string, body: unknown): Observable<T> {
    return this.http.put<T>(`/api${path}`, body);
  }

  /** Upload multipart (FormData): HttpClient imposta da sé il boundary. */
  upload<T>(path: string, form: FormData): Observable<T> {
    return this.http.post<T>(`/api${path}`, form);
  }

  delete<T>(path: string): Observable<T> {
    return this.http.delete<T>(`/api${path}`);
  }
}
