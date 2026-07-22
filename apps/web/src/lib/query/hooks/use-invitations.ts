"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  acceptInvitation,
  createInvitation,
  getInvitationPreview,
  listInvitations,
  revokeInvitation,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";
import type { InvitationCreate, Uuid } from "@/lib/api/types";

export function useInvitationsQuery(householdId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.invitations(householdId ?? ""),
    queryFn: () => listInvitations(householdId as string),
    enabled: Boolean(householdId),
  });
}

export function useCreateInvitationMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: InvitationCreate) => createInvitation(householdId, body),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.invitations(householdId) }),
  });
}

export function useRevokeInvitationMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (invitationId: Uuid) => revokeInvitation(householdId, invitationId),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: queryKeys.invitations(householdId) }),
  });
}

export function useInvitationPreviewQuery(token: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.invitationPreview(token),
    queryFn: () => getInvitationPreview(token),
    enabled,
    retry: false,
  });
}

export function useAcceptInvitationMutation() {
  return useMutation({
    mutationFn: (token: string) => acceptInvitation(token),
  });
}
