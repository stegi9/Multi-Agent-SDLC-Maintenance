import { Routes } from '@angular/router';
import { authGuard } from './core/guards/auth.guard';
import { roleGuard } from './core/guards/role.guard';

export const routes: Routes = [
  {
    path: 'login',
    loadComponent: () => import('./pages/login/login.component').then((m) => m.LoginComponent),
  },
  { path: '', redirectTo: 'dashboard', pathMatch: 'full' },
  {
    path: 'dashboard',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./pages/dashboard/dashboard.component').then((m) => m.DashboardComponent),
  },
  {
    path: 'tickets',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./pages/tickets/tickets.component').then((m) => m.TicketsComponent),
  },
  {
    path: 'work-orders',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./pages/work-orders/work-orders.component').then((m) => m.WorkOrdersComponent),
  },
  {
    path: 'documents',
    canActivate: [authGuard],
    loadComponent: () =>
      import('./pages/documents/documents.component').then((m) => m.DocumentsComponent),
  },
  {
    path: 'settings',
    canActivate: [authGuard, roleGuard('scrum_master')],
    loadComponent: () =>
      import('./pages/settings/settings.component').then((m) => m.SettingsComponent),
  },
  { path: '**', redirectTo: 'dashboard' },
];
