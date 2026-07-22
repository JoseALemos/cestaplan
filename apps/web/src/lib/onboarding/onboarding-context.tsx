"use client";

import type { ReactNode } from "react";
import { createContext, use, useCallback, useMemo, useState } from "react";

import {
  EMPTY_ONBOARDING_STATE,
  type OnboardingBudget,
  type OnboardingHousehold,
  type OnboardingMemberDraft,
  type OnboardingState,
  type OnboardingStore,
} from "./types";
import type { MealRequirementIn, PreferenceIn } from "@/lib/api/types";

const STORAGE_KEY = "cestaplan_onboarding_draft_v1";

function readPersisted(): OnboardingState {
  if (typeof window === "undefined") return EMPTY_ONBOARDING_STATE;
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY_ONBOARDING_STATE;
    return { ...EMPTY_ONBOARDING_STATE, ...(JSON.parse(raw) as OnboardingState) };
  } catch {
    return EMPTY_ONBOARDING_STATE;
  }
}

function persist(state: OnboardingState): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Storage full/unavailable — wizard still works for the current session.
  }
}

interface OnboardingContextValue {
  state: OnboardingState;
  setHousehold: (household: OnboardingHousehold) => void;
  setStore: (store: OnboardingStore) => void;
  addMember: (member: OnboardingMemberDraft) => void;
  updateMember: (localId: string, patch: Partial<OnboardingMemberDraft>) => void;
  removeMember: (localId: string) => void;
  setPreferences: (preferences: PreferenceIn[]) => void;
  setEquipment: (equipment: OnboardingState["equipment"]) => void;
  setBudget: (budget: OnboardingBudget) => void;
  setMealRequirements: (requirements: MealRequirementIn[]) => void;
  reset: () => void;
}

const OnboardingContext = createContext<OnboardingContextValue | null>(null);

export function OnboardingProvider({ children }: { children: ReactNode }) {
  // Lazy initializer: reads the draft once, synchronously, on first render —
  // no separate "hydrate on mount" effect needed. Every mutator below
  // persists inline, so there's no separate "persist on change" effect either.
  const [state, setState] = useState<OnboardingState>(readPersisted);

  const update = useCallback((updater: (prev: OnboardingState) => OnboardingState) => {
    setState((prev) => {
      const next = updater(prev);
      persist(next);
      return next;
    });
  }, []);

  const setHousehold = useCallback(
    (household: OnboardingHousehold) => update((prev) => ({ ...prev, household })),
    [update],
  );

  const setStore = useCallback(
    (store: OnboardingStore) => update((prev) => ({ ...prev, store })),
    [update],
  );

  const addMember = useCallback(
    (member: OnboardingMemberDraft) =>
      update((prev) => ({ ...prev, members: [...prev.members, member] })),
    [update],
  );

  const updateMember = useCallback(
    (localId: string, patch: Partial<OnboardingMemberDraft>) =>
      update((prev) => ({
        ...prev,
        members: prev.members.map((member) =>
          member.localId === localId ? { ...member, ...patch } : member,
        ),
      })),
    [update],
  );

  const removeMember = useCallback(
    (localId: string) =>
      update((prev) => ({
        ...prev,
        members: prev.members.filter((member) => member.localId !== localId),
      })),
    [update],
  );

  const setPreferences = useCallback(
    (preferences: PreferenceIn[]) => update((prev) => ({ ...prev, preferences })),
    [update],
  );

  const setEquipment = useCallback(
    (equipment: OnboardingState["equipment"]) => update((prev) => ({ ...prev, equipment })),
    [update],
  );

  const setBudget = useCallback(
    (budget: OnboardingBudget) => update((prev) => ({ ...prev, budget })),
    [update],
  );

  const setMealRequirements = useCallback(
    (mealRequirements: MealRequirementIn[]) => update((prev) => ({ ...prev, mealRequirements })),
    [update],
  );

  const reset = useCallback(() => {
    setState(EMPTY_ONBOARDING_STATE);
    if (typeof window !== "undefined") {
      window.localStorage.removeItem(STORAGE_KEY);
    }
  }, []);

  const value = useMemo<OnboardingContextValue>(
    () => ({
      state,
      setHousehold,
      setStore,
      addMember,
      updateMember,
      removeMember,
      setPreferences,
      setEquipment,
      setBudget,
      setMealRequirements,
      reset,
    }),
    [
      state,
      setHousehold,
      setStore,
      addMember,
      updateMember,
      removeMember,
      setPreferences,
      setEquipment,
      setBudget,
      setMealRequirements,
      reset,
    ],
  );

  return <OnboardingContext value={value}>{children}</OnboardingContext>;
}

export function useOnboarding(): OnboardingContextValue {
  const context = use(OnboardingContext);
  if (!context) {
    throw new Error("useOnboarding debe usarse dentro de <OnboardingProvider>");
  }
  return context;
}
