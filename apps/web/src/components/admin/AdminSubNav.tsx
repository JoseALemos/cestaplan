"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils/cn";

const TABS = [
  { href: "/admin", label: "Resumen" },
  { href: "/admin/importacion", label: "Importación" },
  { href: "/admin/fuentes", label: "Estado de fuentes" },
  { href: "/admin/ingredient-mappings", label: "Mapeos" },
  { href: "/admin/preparacion", label: "Preparación" },
] as const;

export function AdminSubNav() {
  const pathname = usePathname();

  return (
    <nav aria-label="Administración" className="border-b border-border">
      <ul className="flex gap-1 overflow-x-auto">
        {TABS.map((tab) => {
          const isActive = tab.href === "/admin" ? pathname === "/admin" : pathname.startsWith(tab.href);
          return (
            <li key={tab.href}>
              <Link
                href={tab.href}
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "inline-flex shrink-0 items-center border-b-2 px-3.5 py-3 text-sm font-medium transition-colors duration-fast",
                  isActive
                    ? "border-accent text-ink"
                    : "border-transparent text-ink-muted hover:text-ink",
                )}
              >
                {tab.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
