"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { useAuth } from "@/lib/auth/auth-context";
import { useLogoutMutation } from "@/lib/query/hooks/use-auth-mutations";
import { useIsAdminQuery } from "@/lib/query/hooks/use-admin";

import { Button } from "@/components/ui/Button";

const NAV_LINKS = [
  { href: "/#como-funciona", label: "Cómo funciona" },
  { href: "/#principios", label: "Principios" },
  { href: "/#privacidad", label: "Privacidad" },
];

function BasketMark() {
  return (
    <svg width="30" height="30" viewBox="0 0 512 512" aria-hidden="true">
      <rect width="512" height="512" rx="115" fill="var(--color-primary)" />
      <path d="M176 220 L336 220 L296 380 L216 380 Z" fill="var(--color-bg)" />
      <rect x="110" y="198" width="292" height="30" rx="15" fill="var(--color-bg)" />
      <path
        d="M176 220 Q 256 100 336 220"
        stroke="var(--color-bg)"
        strokeWidth="26"
        fill="none"
        strokeLinecap="round"
      />
      <circle cx="374" cy="168" r="58" fill="var(--color-accent)" />
      <path
        d="M349 168 L367 186 L400 148"
        stroke="var(--color-bg)"
        strokeWidth="17"
        fill="none"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function AuthActions({ onNavigate, layout }: { onNavigate?: () => void; layout: "desktop" | "mobile" }) {
  const router = useRouter();
  const { isAuthenticated, isLoading, user } = useAuth();
  const logoutMutation = useLogoutMutation();
  const { isAdmin } = useIsAdminQuery();

  const containerClass = layout === "desktop" ? "hidden items-center gap-3 md:flex" : "mt-4 flex flex-col gap-2";

  if (isLoading) {
    return <div className={containerClass} aria-hidden="true" />;
  }

  if (isAuthenticated) {
    return (
      <div className={containerClass}>
        {layout === "mobile" ? (
          <p className="px-2 text-sm text-ink-muted">
            {user?.display_name ?? user?.email}
          </p>
        ) : null}
        <Link href="/households" onClick={onNavigate}>
          <Button variant={layout === "desktop" ? "ghost" : "outline"} size={layout === "desktop" ? "sm" : "md"}>
            Mis hogares
          </Button>
        </Link>
        <Link href="/despensa" onClick={onNavigate}>
          <Button variant={layout === "desktop" ? "ghost" : "outline"} size={layout === "desktop" ? "sm" : "md"}>
            Despensa
          </Button>
        </Link>
        <Link href="/precios" onClick={onNavigate}>
          <Button variant={layout === "desktop" ? "ghost" : "outline"} size={layout === "desktop" ? "sm" : "md"}>
            Precios reales
          </Button>
        </Link>
        {isAdmin ? (
          <Link href="/admin" onClick={onNavigate}>
            <Button variant={layout === "desktop" ? "ghost" : "outline"} size={layout === "desktop" ? "sm" : "md"}>
              Administración
            </Button>
          </Link>
        ) : null}
        <Button
          variant="primary"
          size={layout === "desktop" ? "sm" : "md"}
          loading={logoutMutation.isPending}
          onClick={async () => {
            await logoutMutation.mutateAsync();
            onNavigate?.();
            router.push("/");
          }}
        >
          Cerrar sesión
        </Button>
      </div>
    );
  }

  return (
    <div className={containerClass}>
      <Link href="/login" onClick={onNavigate}>
        <Button variant={layout === "desktop" ? "ghost" : "outline"} size={layout === "desktop" ? "sm" : "md"}>
          Iniciar sesión
        </Button>
      </Link>
      <Link href="/registro" onClick={onNavigate}>
        <Button variant="primary" size={layout === "desktop" ? "sm" : "md"}>
          Crear cuenta gratis
        </Button>
      </Link>
    </div>
  );
}

export function SiteHeader() {
  const [menuOpen, setMenuOpen] = useState(false);

  return (
    <header className="sticky top-0 z-40 border-b border-border bg-bg/85 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
        <Link href="/" className="flex items-center gap-2.5 font-display text-lg font-semibold text-ink">
          <BasketMark />
          CestaPlan
        </Link>

        <nav aria-label="Principal" className="hidden items-center gap-7 md:flex">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className="text-sm font-medium text-ink-muted transition-colors hover:text-ink"
            >
              {link.label}
            </Link>
          ))}
        </nav>

        <AuthActions layout="desktop" />

        <button
          type="button"
          className="flex h-10 w-10 items-center justify-center rounded-md text-ink md:hidden"
          aria-expanded={menuOpen}
          aria-controls="menu-movil"
          aria-label={menuOpen ? "Cerrar menú" : "Abrir menú"}
          onClick={() => setMenuOpen((open) => !open)}
        >
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" aria-hidden="true">
            {menuOpen ? (
              <path
                d="M5 5l14 14M19 5L5 19"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
            ) : (
              <path
                d="M4 7h16M4 12h16M4 17h16"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
              />
            )}
          </svg>
        </button>
      </div>

      {menuOpen ? (
        <div id="menu-movil" className="border-t border-border bg-bg px-4 pb-5 pt-3 md:hidden">
          <nav aria-label="Principal (móvil)" className="flex flex-col gap-1">
            {NAV_LINKS.map((link) => (
              <Link
                key={link.href}
                href={link.href}
                className="rounded-md px-2 py-2.5 text-sm font-medium text-ink-muted hover:bg-bg-subtle hover:text-ink"
                onClick={() => setMenuOpen(false)}
              >
                {link.label}
              </Link>
            ))}
          </nav>
          <AuthActions layout="mobile" onNavigate={() => setMenuOpen(false)} />
        </div>
      ) : null}
    </header>
  );
}
