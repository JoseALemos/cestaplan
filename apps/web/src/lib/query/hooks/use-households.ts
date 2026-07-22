"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import {
  addMember,
  createHousehold,
  getEquipment,
  getHousehold,
  listHouseholds,
  listMembers,
  putEquipment,
  updateMember,
} from "@/lib/api/endpoints";
import { queryKeys } from "@/lib/query/keys";
import type { EquipmentSet, HouseholdCreate, MemberCreate, MemberUpdate, Uuid } from "@/lib/api/types";

export function useHouseholdsQuery() {
  return useQuery({ queryKey: queryKeys.households(), queryFn: listHouseholds });
}

export function useHouseholdQuery(householdId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.household(householdId ?? ""),
    queryFn: () => getHousehold(householdId as string),
    enabled: Boolean(householdId),
  });
}

export function useCreateHouseholdMutation() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: HouseholdCreate) => createHousehold(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.households() }),
  });
}

export function useMembersQuery(householdId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.members(householdId ?? ""),
    queryFn: () => listMembers(householdId as string),
    enabled: Boolean(householdId),
  });
}

export function useAddMemberMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: MemberCreate) => addMember(householdId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.members(householdId) }),
  });
}

export function useUpdateMemberMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ memberId, body }: { memberId: Uuid; body: MemberUpdate }) =>
      updateMember(householdId, memberId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.members(householdId) }),
  });
}

export function useEquipmentQuery(householdId: string | null | undefined) {
  return useQuery({
    queryKey: queryKeys.equipment(householdId ?? ""),
    queryFn: () => getEquipment(householdId as string),
    enabled: Boolean(householdId),
  });
}

export function usePutEquipmentMutation(householdId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (body: EquipmentSet) => putEquipment(householdId, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: queryKeys.equipment(householdId) }),
  });
}
