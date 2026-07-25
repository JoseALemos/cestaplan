"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  approveProviderForProduction,
  getProviderPromotionStatus,
  promoteProvider,
} from "@/lib/api/endpoints";

const KEY = "admin-provider-promotion";

export function useProviderPromotionStatusQuery(providerCode: string, enabled = true) {
  return useQuery({
    queryKey: [KEY, "status", providerCode],
    queryFn: () => getProviderPromotionStatus(providerCode),
    enabled: enabled && Boolean(providerCode),
    retry: false,
  });
}

/**
 * Approval and the real (non-dry-run) promotion both change gate eligibility,
 * so both invalidate the status query. The dry-run preview is a separate
 * mutation — it writes nothing, so its own pending/error/success state never
 * collides with the real "Promover" button's.
 */
export function useProviderPromotionActions(providerCode: string) {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: [KEY, "status", providerCode] });

  return {
    approve: useMutation({
      mutationFn: () => approveProviderForProduction(providerCode),
      onSuccess: invalidate,
    }),
    previewPromotion: useMutation({
      mutationFn: () => promoteProvider(providerCode, true),
    }),
    promote: useMutation({
      mutationFn: () => promoteProvider(providerCode, false),
      onSuccess: invalidate,
    }),
  };
}
