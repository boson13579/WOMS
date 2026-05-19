/**
 * Client-state store for the orders filter bar.
 * Server state (the actual order list) lives in React Query — only the UI
 * filter selections, current page, and sort preference belong here.
 */
import { create } from 'zustand';

import type { OrderStatus, SortField } from '../types';

interface OrderFilterState {
  status: OrderStatus | null;
  search: string;
  assignedTo: string[];
  createdBy: string[];
  page: number;
  sortBy: SortField;
  sortOrder: 'asc' | 'desc';
  setStatus: (v: OrderStatus | null) => void;
  setSearch: (v: string) => void;
  setAssignedTo: (v: string[]) => void;
  setCreatedBy: (v: string[]) => void;
  setPage: (v: number) => void;
  setSort: (field: SortField) => void;
  reset: () => void;
}

export const useOrderStore = create<OrderFilterState>()((set) => ({
  status: null,
  search: '',
  assignedTo: [],
  createdBy: [],
  page: 1,
  sortBy: 'order_number',
  sortOrder: 'asc',

  setStatus: (status) => {
    set({ status, page: 1 });
  },
  setSearch: (search) => {
    set({ search, page: 1 });
  },
  setAssignedTo: (assignedTo) => {
    set({ assignedTo, page: 1 });
  },
  setCreatedBy: (createdBy) => {
    set({ createdBy, page: 1 });
  },
  setPage: (page) => {
    set({ page });
  },
  setSort: (field) => {
    set((s) => ({
      sortBy: field,
      sortOrder: s.sortBy === field && s.sortOrder === 'asc' ? 'desc' : 'asc',
      page: 1,
    }));
  },
  reset: () => {
    set({
      status: null,
      search: '',
      assignedTo: [],
      createdBy: [],
      page: 1,
      sortBy: 'order_number',
      sortOrder: 'asc',
    });
  },
}));
