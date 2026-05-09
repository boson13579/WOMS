import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Search, ShieldCheck, UserCog } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { useAuthStore } from '@/features/auth/stores/authStore';

import { deactivateUser, listUsers, updateUser } from '../api/users';
import { userRoleSchema, type UserResponse, type UserRole } from '../types/user';

interface EditState {
  user: UserResponse;
  role: UserRole;
  isActive: boolean;
}

const ROLE_LABELS: Record<UserRole, string> = {
  root: 'Root',
  scheduler: 'Scheduler',
  order_manager: 'Order Manager',
  viewer: 'Viewer',
};

function roleBadgeVariant(role: UserRole): 'destructive' | 'secondary' | 'outline' {
  if (role === 'root') {
    return 'destructive';
  }
  if (role === 'viewer') {
    return 'outline';
  }
  return 'secondary';
}

export function AdminUsersPage(): JSX.Element {
  const queryClient = useQueryClient();
  const currentRole = useAuthStore((state) => state.user?.role);
  const [search, setSearch] = useState('');
  const [edit, setEdit] = useState<EditState | null>(null);

  const usersQuery = useQuery({
    queryKey: ['users', search],
    queryFn: () => listUsers(search),
    enabled: currentRole === 'root',
  });

  const updateMutation = useMutation({
    mutationFn: (state: EditState) =>
      updateUser(state.user.id, {
        role: state.role,
        is_active: state.isActive,
        version_id: state.user.version_id,
      }),
    onSuccess: () => {
      setEdit(null);
      void queryClient.invalidateQueries({ queryKey: ['users'] });
    },
  });

  const deactivateMutation = useMutation({
    mutationFn: deactivateUser,
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['users'] });
    },
  });

  const errorMessage = useMemo(() => {
    const error = usersQuery.error ?? updateMutation.error ?? deactivateMutation.error;
    return error instanceof Error ? error.message : null;
  }, [deactivateMutation.error, updateMutation.error, usersQuery.error]);

  if (currentRole !== 'root') {
    return (
      <div className="mx-auto max-w-2xl px-6 py-10">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <ShieldCheck className="h-5 w-5" aria-hidden="true" />
              Root access required
            </CardTitle>
          </CardHeader>
          <CardContent className="text-sm text-muted-foreground">
            User management is available only to root users.
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6 px-6 py-6">
      <div className="flex flex-col gap-3 md:flex-row md:items-end md:justify-between">
        <div>
          <h2 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <UserCog className="h-5 w-5" aria-hidden="true" />
            User Management
          </h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Manage account roles and active status.
          </p>
        </div>

        <div className="w-full max-w-sm space-y-2">
          <Label htmlFor="user-search">Search users</Label>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              id="user-search"
              value={search}
              onChange={(event) => {
                setSearch(event.target.value);
              }}
              placeholder="Username or email"
              className="pl-9"
            />
          </div>
        </div>
      </div>

      {errorMessage ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/30 bg-destructive/10 px-4 py-3 text-sm text-destructive"
        >
          {errorMessage}
        </div>
      ) : null}

      <Card>
        <CardContent className="p-0">
          <Table className="min-w-[760px]">
            <TableHeader className="bg-muted/40 text-xs uppercase">
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Status</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {usersQuery.isLoading ? (
                <TableRow>
                  <TableCell className="py-8 text-center text-muted-foreground" colSpan={5}>
                    Loading users...
                  </TableCell>
                </TableRow>
              ) : null}

              {usersQuery.data?.users.map((user) => {
                const isEditing = edit?.user.id === user.id;
                return (
                  <TableRow key={user.id}>
                    <TableCell className="font-medium">{user.username}</TableCell>
                    <TableCell className="text-muted-foreground">
                      {user.email ?? 'No email'}
                    </TableCell>
                    <TableCell>
                      {isEditing ? (
                        <select
                          aria-label={`Role for ${user.username}`}
                          value={edit.role}
                          onChange={(event) => {
                            const nextRole = userRoleSchema.parse(event.target.value);
                            setEdit((current) =>
                              current ? { ...current, role: nextRole } : current,
                            );
                          }}
                          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
                        >
                          {userRoleSchema.options.map((role) => (
                            <option key={role} value={role}>
                              {ROLE_LABELS[role]}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <Badge variant={roleBadgeVariant(user.role)}>
                          {ROLE_LABELS[user.role]}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      {isEditing ? (
                        <div className="inline-flex items-center gap-2">
                          <Input
                            id={`user-active-${user.id}`}
                            type="checkbox"
                            checked={edit.isActive}
                            onChange={(event) => {
                              setEdit((current) =>
                                current ? { ...current, isActive: event.target.checked } : current,
                              );
                            }}
                            className="h-4 w-4 p-0 accent-primary"
                          />
                          <Label htmlFor={`user-active-${user.id}`} className="text-sm">
                            Active
                          </Label>
                        </div>
                      ) : (
                        <Badge variant={user.is_active ? 'success' : 'outline'}>
                          {user.is_active ? 'Active' : 'Inactive'}
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>
                      <div className="flex justify-end gap-2">
                        {isEditing ? (
                          <>
                            <Button
                              type="button"
                              size="sm"
                              onClick={() => {
                                updateMutation.mutate(edit);
                              }}
                              disabled={updateMutation.isPending}
                            >
                              Save
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={() => {
                                setEdit(null);
                              }}
                            >
                              Cancel
                            </Button>
                          </>
                        ) : (
                          <>
                            <Button
                              type="button"
                              size="sm"
                              variant="outline"
                              onClick={() => {
                                setEdit({
                                  user,
                                  role: user.role,
                                  isActive: user.is_active,
                                });
                              }}
                            >
                              Edit
                            </Button>
                            <Button
                              type="button"
                              size="sm"
                              variant="destructive"
                              onClick={() => {
                                deactivateMutation.mutate(user.id);
                              }}
                              disabled={!user.is_active || deactivateMutation.isPending}
                            >
                              Deactivate
                            </Button>
                          </>
                        )}
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}

              {usersQuery.data?.users.length === 0 ? (
                <TableRow>
                  <TableCell className="py-8 text-center text-muted-foreground" colSpan={5}>
                    No users found.
                  </TableCell>
                </TableRow>
              ) : null}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
