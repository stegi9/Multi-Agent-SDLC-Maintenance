import { inject } from '@angular/core';
import { CanActivateFn, Router } from '@angular/router';
import { AuthService } from '../services/auth.service';

/** Consente l'accesso solo agli utenti con il ruolo indicato. */
export function roleGuard(role: string): CanActivateFn {
  return () => {
    const auth = inject(AuthService);
    const router = inject(Router);
    if (auth.hasRole(role)) {
      return true;
    }
    return router.createUrlTree(['/dashboard']);
  };
}
