"use client";

import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  approveMapping,
  bulkApproveMappings,
  bulkRejectMappings,
  enrichMapping,
  getMappingCandidate,
  getMappingSummary,
  listMappingCandidates,
  rejectMapping,
  revokeMapping,
} from "@/lib/api/endpoints";
import type { MappingCandidateFilters } from "@/lib/api/types";

const KEY = "admin-mappings";

export function useMappingCandidatesQuery(filters: MappingCandidateFilters, enabled = true) {
  return useQuery({
    queryKey: [KEY, "candidates", filters],
    queryFn: () => listMappingCandidates(filters),
    enabled,
    placeholderData: keepPreviousData,
    retry: false,
  });
}

export function useMappingCandidateQuery(mappingId: number | null) {
  return useQuery({
    queryKey: [KEY, "detail", mappingId],
    queryFn: () => getMappingCandidate(mappingId as number),
    enabled: mappingId !== null,
    retry: false,
  });
}

export function useMappingSummaryQuery(providerCode: string, enabled = true) {
  return useQuery({
    queryKey: [KEY, "summary", providerCode],
    queryFn: () => getMappingSummary(providerCode),
    enabled: enabled && Boolean(providerCode),
    retry: false,
  });
}

/** All mutations invalidate the whole mapping cache so the table + detail refresh together. */
export function useMappingActions() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: [KEY] });

  return {
    approve: useMutation({
      mutationFn: (v: { id: number; reason?: string }) => approveMapping(v.id, v.reason),
      onSuccess: invalidate,
    }),
    reject: useMutation({
      mutationFn: (v: { id: number; reason: string }) => rejectMapping(v.id, v.reason),
      onSuccess: invalidate,
    }),
    revoke: useMutation({
      mutationFn: (v: { id: number; reason: string }) => revokeMapping(v.id, v.reason),
      onSuccess: invalidate,
    }),
    enrich: useMutation({
      mutationFn: (id: number) => enrichMapping(id),
      onSuccess: invalidate,
    }),
    bulkApprove: useMutation({
      mutationFn: (v: { ids: number[]; reason?: string }) => bulkApproveMappings(v.ids, v.reason),
      onSuccess: invalidate,
    }),
    bulkReject: useMutation({
      mutationFn: (v: { ids: number[]; reason: string }) => bulkRejectMappings(v.ids, v.reason),
      onSuccess: invalidate,
    }),
  };
}
