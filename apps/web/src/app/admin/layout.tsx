import type { ReactNode } from "react";

import { AdminGate } from "@/components/admin/AdminGate";
import { AdminSubNav } from "@/components/admin/AdminSubNav";

export const metadata = {
  title: "Administración",
};

export default function AdminLayout({ children }: { children: ReactNode }) {
  return (
    <AdminGate>
      <div className="mx-auto flex max-w-5xl flex-col gap-6 px-4 py-8 sm:px-6">
        <AdminSubNav />
        {children}
      </div>
    </AdminGate>
  );
}
