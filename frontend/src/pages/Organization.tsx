import { useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  ChevronDown,
  ChevronRight,
  Network,
  Pencil,
  Plus,
  Users,
} from "lucide-react";
import { api, ApiError, errorText } from "../api";
import { useActor } from "../App";
import EmployeeAvatar from "../EmployeeAvatar";
import {
  PageTitle,
  useResource,
  Loading,
  ErrorBox,
  Empty,
  Modal,
  Field,
  SearchInput,
  Success,
} from "../ui";
import {
  organizationTypes,
  organizationTree,
  unassignedEmployees,
  organizationEmployeeMatches,
  filterOrganizationTree,
  organizationUnitPath,
  organizationDescendants,
  organizationManagerCandidates,
  organizationEmployeeUrl,
  organizationUnitCompatible,
  organizationAssignmentPayload,
  organizationUnitPatchPayload,
  organizationSiteRequired,
  organizationWritableSites,
  type OrganizationEmployee,
  type OrganizationUnit,
  type OrganizationNode,
} from "../organization";
import "./Organization.css";

type Directory = {
  units: OrganizationUnit[];
  employees: OrganizationEmployee[];
  can_edit: boolean;
};
export function OrganizationPerson({
  employee,
  onOpen,
}: {
  employee: OrganizationEmployee;
  onOpen: (id: string) => void;
}) {
  return (
    <button
      type="button"
      className="org-person"
      onClick={() => onOpen(employee.id)}
      aria-label={`Открыть сотрудника: ${employee.name}`}
    >
      <EmployeeAvatar
        name={employee.name}
        src={employee.profile_photo_url}
        small
      />
      <span>
        <strong>{employee.name}</strong>
        <small>{employee.position || employee.department || "Сотрудник"}</small>
      </span>
    </button>
  );
}
function OrganizationBranch({
  node,
  employees,
  canEdit,
  expanded,
  onToggle,
  onOpen,
  onEdit,
  onCreate,
  forceExpanded,
  depth = 0,
}: {
  node: OrganizationNode;
  employees: OrganizationEmployee[];
  canEdit: boolean;
  expanded: Set<string>;
  onToggle: (id: string) => void;
  onOpen: (id: string) => void;
  onEdit: (unit: OrganizationUnit) => void;
  onCreate: (parent: string) => void;
  forceExpanded: boolean;
  depth?: number;
}) {
  const open = forceExpanded || expanded.has(node.unit.id),
    head = employees.find(
      (employee) => employee.id === node.unit.head_employee_id,
    );
  return (
    <section className={depth >= 3 ? "org-unit org-unit-deep" : "org-unit"}>
      <div className="org-unit-heading">
        <button
          type="button"
          className="org-unit-expand"
          aria-expanded={open}
          aria-controls={`org-members-${node.unit.id}`}
          onClick={() => onToggle(node.unit.id)}
        >
          {open ? <ChevronDown size={19} /> : <ChevronRight size={19} />}
          <span>
            <small>{organizationTypes[node.unit.type]}</small>
            <strong>{node.unit.title}</strong>
          </span>
          <span className="org-member-count">
            <Users size={15} />
            {node.members.length}
          </span>
        </button>
        {canEdit && node.unit.can_edit !== false && (
          <div className="org-unit-actions">
            <button
              type="button"
              className="icon-button"
              aria-label={`Изменить подразделение: ${node.unit.title}`}
              onClick={() => onEdit(node.unit)}
            >
              <Pencil size={17} />
            </button>
            <button
              type="button"
              className="icon-button"
              aria-label={`Добавить подразделение в ${node.unit.title}`}
              onClick={() => onCreate(node.unit.id)}
            >
              <Plus size={18} />
            </button>
          </div>
        )}
      </div>
      {head && (
        <div className="org-head">
          Руководитель:{" "}
          <button
            className="text-button"
            type="button"
            onClick={() => onOpen(head.id)}
          >
            {head.name}
          </button>
        </div>
      )}
      {open && (
        <div id={`org-members-${node.unit.id}`} className="org-unit-body">
          {node.members.length > 0 && (
            <div className="org-people">
              {node.members.map((employee) => (
                <OrganizationPerson
                  key={employee.id}
                  employee={employee}
                  onOpen={onOpen}
                />
              ))}
            </div>
          )}
          {node.children.map((child) => (
            <OrganizationBranch
              key={child.unit.id}
              node={child}
              employees={employees}
              canEdit={canEdit}
              expanded={expanded}
              onToggle={onToggle}
              onOpen={onOpen}
              onEdit={onEdit}
              onCreate={onCreate}
              forceExpanded={forceExpanded}
              depth={depth + 1}
            />
          ))}
          {!node.members.length && !node.children.length && (
            <p className="org-empty">Нет сотрудников</p>
          )}
        </div>
      )}
    </section>
  );
}
export default function Organization() {
  const resource = useResource<Directory>("/organization");
  const [params, setParams] = useSearchParams();
  const [query, setQuery] = useState(""),
    [expanded, setExpanded] = useState<Set<string>>(new Set()),
    [editUnit, setEditUnit] = useState<OrganizationUnit | null>(null),
    [createParent, setCreateParent] = useState<string | null>(null),
    [creating, setCreating] = useState(false),
    [message, setMessage] = useState("");
  const units = resource.data?.units || [],
    employees = resource.data?.employees || [],
    canEdit = resource.data?.can_edit === true;
  const roots = filterOrganizationTree(
      organizationTree(units, employees),
      query,
    ),
    unassigned = unassignedEmployees(units, employees).filter((employee) =>
      organizationEmployeeMatches(employee, query),
    );
  const employeeId = params.get("employee");
  const openEmployee = (id: string) => setParams({ employee: id });
  const closeEmployee = () => setParams({});
  const saved = (text: string) => {
    resource.reload();
    setMessage(text);
  };
  return (
    <div className="organization-page">
      <PageTitle
        title="Оргструктура"
        action={
          canEdit ? (
            <button
              className="button primary"
              onClick={() => {
                setCreateParent(null);
                setCreating(true);
              }}
            >
              <Plus size={18} />
              Добавить подразделение
            </button>
          ) : undefined
        }
      />
      <ErrorBox error={resource.error} />
      {message && <Success>{message}</Success>}
      <div className="org-search">
        <SearchInput
          value={query}
          onChange={setQuery}
          placeholder="Найти подразделение или сотрудника"
        />
      </div>
      {resource.loading ? (
        <Loading />
      ) : (
        <div className="org-tree">
          {roots.map((node) => (
            <OrganizationBranch
              key={node.unit.id}
              node={node}
              employees={employees}
              canEdit={canEdit}
              expanded={expanded}
              onToggle={(id) =>
                setExpanded((before) => {
                  const next = new Set(before);
                  next.has(id) ? next.delete(id) : next.add(id);
                  return next;
                })
              }
              onOpen={openEmployee}
              onEdit={setEditUnit}
              onCreate={(parent) => {
                setCreateParent(parent);
                setCreating(true);
              }}
              forceExpanded={!!query.trim()}
            />
          ))}
          {unassigned.length > 0 && (
            <section className="org-unassigned panel">
              <h2>
                <Users size={20} />
                Без подразделения{" "}
                <span className="org-member-count">{unassigned.length}</span>
              </h2>
              <div className="org-people">
                {unassigned.map((employee) => (
                  <OrganizationPerson
                    key={employee.id}
                    employee={employee}
                    onOpen={openEmployee}
                  />
                ))}
              </div>
            </section>
          )}
          {!roots.length && !unassigned.length && (
            <Empty
              title={query ? "Ничего не найдено" : "Подразделений пока нет"}
            />
          )}
        </div>
      )}
      {(creating || editUnit) && (
        <UnitEditor
          key={editUnit?.id || `new-${createParent}`}
          unit={editUnit}
          parentId={createParent}
          units={units}
          employees={employees}
          onClose={() => {
            setCreating(false);
            setEditUnit(null);
          }}
          onSaved={() => {
            setCreating(false);
            setEditUnit(null);
            saved("Подразделение сохранено");
          }}
        />
      )}
      {employeeId && (
        <DirectoryEmployee
          key={employeeId}
          id={employeeId}
          units={units}
          employees={employees}
          canEdit={canEdit}
          onClose={closeEmployee}
          onOpen={openEmployee}
          onSaved={() => saved("Привязки сотрудника сохранены")}
        />
      )}
    </div>
  );
}
function UnitEditor({
  unit,
  parentId,
  units,
  employees,
  onClose,
  onSaved,
}: {
  unit: OrganizationUnit | null;
  parentId: string | null;
  units: OrganizationUnit[];
  employees: OrganizationEmployee[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const actor = useActor();
  const directory = useResource<any>("/directory"),
    inherited = units.find((candidate) => candidate.id === parentId);
  const [title, setTitle] = useState(unit?.title || ""),
    [type, setType] = useState<OrganizationUnit["type"]>(
      unit?.type || "DEPARTMENT",
    ),
    [parent, setParent] = useState(unit?.parent_id || parentId || ""),
    [head, setHead] = useState(unit?.head_employee_id || ""),
    [legalEntity, setLegalEntity] = useState(
      unit?.legal_entity_id || inherited?.legal_entity_id || "",
    ),
    [site, setSite] = useState(unit?.site_id || inherited?.site_id || ""),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [etag, setEtag] = useState(unit?.etag || null),
    [conflict, setConflict] = useState(false),
    [baseline, setBaseline] = useState(unit);
  const unitDraft = {
    title: title.trim(),
    type,
    parent_id: parent || null,
    head_employee_id: head || null,
    legal_entity_id: legalEntity,
    site_id: site || null,
  };
  const changes = baseline
    ? organizationUnitPatchPayload(unitDraft, baseline)
    : unitDraft;
  const unchanged = !!baseline && Object.keys(changes).length === 0;
  const parentUnit = units.find((candidate) => candidate.id === parent);
  const requireSite = organizationSiteRequired(
    actor.scope || {},
    parentUnit?.site_id,
  );
  const availableSites = organizationWritableSites(
    directory.data?.sites || [],
    legalEntity,
    actor.scope || {},
    parentUnit?.site_id,
  );
  const excluded = unit
    ? organizationDescendants(units, unit.id)
    : new Set<string>();
  return (
    <Modal
      title={unit ? "Изменить подразделение" : "Добавить подразделение"}
      onClose={onClose}
    >
      <form
        className="org-unit-form"
        onSubmit={async (event) => {
          event.preventDefault();
          if (unchanged) return;
          setBusy(true);
          setError("");
          try {
            if (unit)
              await api.patch(`/organization/units/${unit.id}`, changes, etag);
            else await api.post("/organization/units", unitDraft);
            onSaved();
          } catch (e) {
            setError(errorText(e));
            if (e instanceof ApiError && e.status === 412) setConflict(true);
          } finally {
            setBusy(false);
          }
        }}
      >
        <fieldset disabled={busy}>
          <Field label="Название">
            <input
              required
              maxLength={160}
              value={title}
              onChange={(e) => setTitle(e.target.value)}
            />
          </Field>
          <Field label="Тип подразделения">
            <select
              value={type}
              onChange={(e) =>
                setType(e.target.value as OrganizationUnit["type"])
              }
            >
              {Object.entries(organizationTypes).map(([value, label]) => (
                <option key={value} value={value}>
                  {label}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Юридическое лицо">
            <select
              required
              value={legalEntity}
              onChange={(e) => {
                setLegalEntity(e.target.value);
                setSite("");
                setParent("");
                setHead("");
              }}
            >
              <option value="">Выберите юридическое лицо</option>
              {(directory.data?.legal_entities || []).map((entity: any) => (
                <option key={entity.id} value={entity.id}>
                  {entity.name || entity.title}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Площадка">
            <select
              required={requireSite}
              value={site}
              onChange={(e) => setSite(e.target.value)}
            >
              <option value="">
                {requireSite
                  ? "Выберите площадку"
                  : "Все площадки юридического лица"}
              </option>
              {availableSites.map((candidate: any) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Входит в подразделение">
            <select
              value={parent}
              onChange={(e) => {
                setParent(e.target.value);
                const selected = units.find(
                  (candidate) => candidate.id === e.target.value,
                );
                if (selected) {
                  setLegalEntity(selected.legal_entity_id || "");
                  if (selected.site_id) setSite(selected.site_id);
                }
              }}
            >
              <option value="">Верхний уровень</option>
              {units
                .filter(
                  (candidate) =>
                    candidate.status !== "ARCHIVED" &&
                    !excluded.has(candidate.id) &&
                    (!legalEntity ||
                      candidate.legal_entity_id === legalEntity) &&
                    (!candidate.site_id || !site || candidate.site_id === site),
                )
                .map((candidate) => (
                  <option key={candidate.id} value={candidate.id}>
                    {candidate.title}
                  </option>
                ))}
            </select>
          </Field>
          <Field label="Руководитель подразделения">
            <select value={head} onChange={(e) => setHead(e.target.value)}>
              <option value="">Не назначен</option>
              {employees
                .filter(
                  (employee) =>
                    !legalEntity || employee.legal_entity_id === legalEntity,
                )
                .map((employee) => (
                  <option key={employee.id} value={employee.id}>
                    {employee.name}
                  </option>
                ))}
            </select>
          </Field>
          <ErrorBox error={directory.error || error} />
          {conflict && unit && (
            <button
              type="button"
              className="button secondary"
              onClick={async () => {
                setBusy(true);
                try {
                  const response = await api.get<Directory>("/organization");
                  const current = response.data.units.find(
                    (row) => row.id === unit.id,
                  );
                  if (!current || current.can_edit === false) {
                    setError(
                      "Подразделение больше недоступно для редактирования",
                    );
                    return;
                  }
                  setTitle(current.title);
                  setType(current.type);
                  setParent(current.parent_id || "");
                  setHead(current.head_employee_id || "");
                  setLegalEntity(current.legal_entity_id || "");
                  setSite(current.site_id || "");
                  setEtag(current.etag || null);
                  setBaseline(current);
                  setConflict(false);
                  setError("");
                } catch (e) {
                  setError(errorText(e));
                } finally {
                  setBusy(false);
                }
              }}
            >
              Загрузить актуальные данные
            </button>
          )}
          <div className="modal-actions">
            <button
              className="button primary"
              disabled={
                busy ||
                unchanged ||
                conflict ||
                !title.trim() ||
                !legalEntity ||
                (requireSite && !site)
              }
            >
              Сохранить
            </button>
          </div>
        </fieldset>
      </form>
    </Modal>
  );
}
export function DirectoryRelationships({
  managerChain,
  directReports,
  onOpen,
}: {
  managerChain: OrganizationEmployee[];
  directReports: OrganizationEmployee[];
  onOpen: (id: string) => void;
}) {
  return (
    <div className="org-relationships">
      <section>
        <h3>Руководители</h3>
        {managerChain.length ? (
          <ol className="org-manager-chain">
            {managerChain.map((employee, index) => (
              <li key={employee.id}>
                <span className="org-manager-level">
                  {index === 0
                    ? "Непосредственный руководитель"
                    : "Руководитель выше"}
                </span>
                <OrganizationPerson employee={employee} onOpen={onOpen} />
              </li>
            ))}
          </ol>
        ) : (
          <p className="org-empty">Руководитель не назначен</p>
        )}
      </section>
      <section>
        <h3>Прямые подчинённые</h3>
        {directReports.length ? (
          <div className="org-people">
            {directReports.map((employee) => (
              <OrganizationPerson
                key={employee.id}
                employee={employee}
                onOpen={onOpen}
              />
            ))}
          </div>
        ) : (
          <p className="org-empty">Нет прямых подчинённых</p>
        )}
      </section>
    </div>
  );
}
export function DirectoryEmployee({
  id,
  units,
  employees,
  canEdit,
  onClose,
  onOpen,
  onSaved,
}: {
  id: string;
  units: OrganizationUnit[];
  employees: OrganizationEmployee[];
  canEdit: boolean;
  onClose: () => void;
  onOpen: (id: string) => void;
  onSaved: () => void;
}) {
  const resource = useResource<any>(
    `/organization/employees/${encodeURIComponent(id)}`,
  );
  const [editing, setEditing] = useState(false),
    [error, setError] = useState("");
  const [roleConfirmation, setRoleConfirmation] = useState<
      "MANAGER" | "EMPLOYEE" | null
    >(null),
    [roleBusy, setRoleBusy] = useState(false),
    [roleConflict, setRoleConflict] = useState(false);
  const employee: OrganizationEmployee | undefined =
    resource.data?.employee || resource.data;
  const chain: OrganizationEmployee[] = resource.data?.manager_chain || [],
    direct: OrganizationEmployee[] = resource.data?.direct_reports || [];
  const path = organizationUnitPath(units, employee?.org_unit_id);
  return (
    <Modal title={employee?.name || "Сотрудник"} onClose={onClose} wide>
      <div className="org-profile">
        <ErrorBox error={resource.error || error} />
        {resource.loading ? (
          <Loading />
        ) : employee?.id ? (
          <>
            <div className="org-profile-heading">
              <EmployeeAvatar
                name={employee.name}
                src={employee.profile_photo_url}
              />
              <div>
                <h3>{employee.name}</h3>
                <p>{employee.position || "Должность не указана"}</p>
              </div>
            </div>
            <div className="org-unit-path">
              <Network size={17} />
              <span>
                {path.map((unit) => unit.title).join(" / ") ||
                  employee.department ||
                  "Без подразделения"}
              </span>
            </div>
            {canEdit && employee.can_edit !== false && (
              <button
                className="button secondary"
                onClick={() => {
                  setEditing(!editing);
                  setError("");
                }}
              >
                <Pencil size={16} />
                {editing
                  ? "Закрыть редактирование"
                  : "Изменить подразделение и руководителя"}
              </button>
            )}
            {canEdit &&
              employee.can_change_access_role &&
              !roleConfirmation && (
                <button
                  className="button secondary"
                  onClick={() => {
                    setRoleConfirmation(
                      employee.access_role === "MANAGER"
                        ? "EMPLOYEE"
                        : "MANAGER",
                    );
                    setRoleConflict(false);
                    setError("");
                  }}
                >
                  <Users size={16} />
                  {employee.access_role === "MANAGER"
                    ? "Убрать роль руководителя"
                    : "Сделать руководителем"}
                </button>
              )}
            {roleConfirmation && (
              <section className="panel">
                <h3>
                  {roleConfirmation === "MANAGER"
                    ? "Предоставить доступ руководителя?"
                    : "Оставить доступ сотрудника?"}
                </h3>
                <p>
                  {roleConfirmation === "MANAGER"
                    ? "В миниапе появятся профиль и команда прямых подчинённых."
                    : "В миниапе останется собственный профиль сотрудника."}{" "}
                  Подразделение, руководитель и история сохранятся. Потребуется
                  заново войти в кабинет.
                </p>
                {roleConflict ? (
                  <button
                    className="button secondary"
                    onClick={() => {
                      resource.reload();
                      setRoleConfirmation(null);
                      setError("");
                    }}
                  >
                    Обновить карточку
                  </button>
                ) : (
                  <div className="button-row">
                    <button
                      className="button secondary"
                      disabled={roleBusy}
                      onClick={() => setRoleConfirmation(null)}
                    >
                      Отмена
                    </button>
                    <button
                      className="button primary"
                      disabled={roleBusy || !(resource.etag || employee.etag)}
                      onClick={async () => {
                        setRoleBusy(true);
                        setError("");
                        try {
                          await api.post(
                            `/organization/employees/${employee.id}/access-role`,
                            { role: roleConfirmation },
                            resource.etag || employee.etag,
                          );
                          setRoleConfirmation(null);
                          resource.reload();
                          onSaved();
                        } catch (e) {
                          setError(errorText(e));
                          if (e instanceof ApiError && e.status === 412)
                            setRoleConflict(true);
                        } finally {
                          setRoleBusy(false);
                        }
                      }}
                    >
                      {roleBusy ? "Сохраняем…" : "Подтвердить"}
                    </button>
                  </div>
                )}
              </section>
            )}
            {editing && (
              <EmployeeAssignment
                key={`${id}-${employee.version}`}
                employee={employee}
                etag={resource.etag || employee.etag || null}
                units={units}
                employees={employees}
                onError={setError}
                onReload={() => {
                  setError("");
                  resource.reload();
                }}
                onSaved={() => {
                  setEditing(false);
                  resource.reload();
                  onSaved();
                }}
              />
            )}
            <DirectoryRelationships
              managerChain={chain}
              directReports={direct}
              onOpen={onOpen}
            />
          </>
        ) : (
          !resource.error && <Empty title="Сотрудник не найден" />
        )}
      </div>
    </Modal>
  );
}
function EmployeeAssignment({
  employee,
  etag,
  units,
  employees,
  onError,
  onReload,
  onSaved,
}: {
  employee: OrganizationEmployee;
  etag: string | null;
  units: OrganizationUnit[];
  employees: OrganizationEmployee[];
  onError: (error: string) => void;
  onReload: () => void;
  onSaved: () => void;
}) {
  const [conflict, setConflict] = useState(false);
  const [unit, setUnit] = useState(employee.org_unit_id || ""),
    [manager, setManager] = useState(employee.reports_to_employee_id || ""),
    [busy, setBusy] = useState(false);
  const changes = organizationAssignmentPayload(unit, manager, employee);
  const unchanged = Object.keys(changes).length === 0;
  return (
    <form
      className="org-assignment"
      onSubmit={async (event) => {
        event.preventDefault();
        if (unchanged) return;
        setBusy(true);
        onError("");
        try {
          await api.patch(
            `/organization/employees/${employee.id}`,
            changes,
            etag,
          );
          onSaved();
        } catch (e) {
          onError(errorText(e));
          if (e instanceof ApiError && e.status === 412) setConflict(true);
        } finally {
          setBusy(false);
        }
      }}
    >
      <fieldset disabled={busy}>
        <Field label="Подразделение">
          <select value={unit} onChange={(e) => setUnit(e.target.value)}>
            <option value="">Без подразделения</option>
            {units
              .filter((candidate) =>
                organizationUnitCompatible(candidate, employee),
              )
              .map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.title}
                </option>
              ))}
          </select>
        </Field>
        <Field label="Непосредственный руководитель">
          <select value={manager} onChange={(e) => setManager(e.target.value)}>
            <option value="">Не назначен вручную</option>
            {organizationManagerCandidates(employees, employee.id)
              .filter(
                (candidate) =>
                  !employee.legal_entity_id ||
                  candidate.legal_entity_id === employee.legal_entity_id,
              )
              .map((candidate) => (
                <option key={candidate.id} value={candidate.id}>
                  {candidate.name}
                </option>
              ))}
          </select>
        </Field>
        {conflict && (
          <button type="button" className="button secondary" onClick={onReload}>
            Загрузить актуальные данные
          </button>
        )}
        <button
          className="button primary"
          disabled={busy || conflict || unchanged}
        >
          Сохранить привязки
        </button>
      </fieldset>
    </form>
  );
}
export function EmployeeOrganizationLink({ id }: { id: string }) {
  const profile = useResource<any>(
    `/organization/employees/${encodeURIComponent(id)}`,
  );
  const employee = profile.data?.employee || profile.data;
  const manager = profile.data?.manager_chain?.[0];
  return (
    <section className="panel inset org-employee-link">
      <h3>Оргструктура</h3>
      <ErrorBox error={profile.error} />
      <div className="org-employee-link-facts">
        <span>
          Подразделение:{" "}
          {profile.loading
            ? "Загружается…"
            : employee?.org_unit_title ||
              employee?.department ||
              "Без подразделения"}
        </span>
        <span>
          Руководитель:{" "}
          {manager ? (
            <Link to={organizationEmployeeUrl(manager.id)}>{manager.name}</Link>
          ) : (
            "Не назначен"
          )}
        </span>
      </div>
      <Link className="button secondary" to={organizationEmployeeUrl(id)}>
        Открыть в оргструктуре
      </Link>
    </section>
  );
}
