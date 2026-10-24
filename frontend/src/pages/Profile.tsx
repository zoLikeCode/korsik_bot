import { useActor } from "../App";
import EmployeeAvatar from "../EmployeeAvatar";
import { ErrorBox, Loading, PageTitle, useResource } from "../ui";
import "./Profile.css";

export type ProfilePerson = {
  id?: string;
  name: string;
  email?: string;
  department?: string;
  position?: string;
  profile_photo_url?: string | null;
  start_date?: string | null;
  probation_end_date?: string | null;
  is_adapting?: boolean;
};
export type PersonalProfile = {
  user: { id: string; name: string; email: string | null; role: string };
  employee: ProfilePerson | null;
  manager: ProfilePerson | null;
  direct_reports: ProfilePerson[];
};
export function profileDate(value?: string | null) {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return "Не указана";
  const date = new Date(`${value}T12:00:00Z`);
  return Number.isNaN(date.getTime()) ||
    date.toISOString().slice(0, 10) !== value
    ? "Не указана"
    : new Intl.DateTimeFormat("ru-RU", {
        day: "numeric",
        month: "long",
        year: "numeric",
        timeZone: "UTC",
      }).format(date);
}
export function ProfileContent({
  data,
  managerView,
}: {
  data: PersonalProfile;
  managerView: boolean;
}) {
  const person = data.employee || data.user;
  const employee = data.employee;
  return (
    <div className="personal-profile">
      <section className="panel profile-identity">
        <EmployeeAvatar name={person.name} src={employee?.profile_photo_url} />
        <div>
          <h2>{person.name}</h2>
          <p>
            {employee?.position ||
              (managerView ? "Руководитель подразделения" : "Сотрудник")}
          </p>
        </div>
        {employee?.is_adapting && (
          <span className="profile-adapting">Проходит адаптацию</span>
        )}
        <dl className="profile-fields">
          <div>
            <dt>Рабочая почта</dt>
            <dd>{employee?.email || data.user.email || "Не указана"}</dd>
          </div>
          <div>
            <dt>Подразделение</dt>
            <dd>{employee?.department || "Не указано"}</dd>
          </div>
          {!managerView && (
            <>
              <div>
                <dt>Дата выхода на работу</dt>
                <dd>{profileDate(employee?.start_date)}</dd>
              </div>
              <div>
                <dt>Окончание испытательного срока</dt>
                <dd>{profileDate(employee?.probation_end_date)}</dd>
              </div>
            </>
          )}
        </dl>
        {!employee && (
          <p className="muted profile-missing-card">
            Кадровая карточка пока не связана с аккаунтом.
          </p>
        )}
      </section>
      {!managerView && (
        <section className="panel">
          <h2>Руководитель</h2>
          {data.manager ? (
            <div className="profile-person-row">
              <EmployeeAvatar
                name={data.manager.name}
                src={data.manager.profile_photo_url}
              />
              <div>
                <strong>{data.manager.name}</strong>
                {data.manager.position && <p>{data.manager.position}</p>}
              </div>
            </div>
          ) : (
            <p className="muted">Руководитель пока не назначен.</p>
          )}
        </section>
      )}
      {managerView && (
        <section className="panel">
          <h2>
            Моя команда{" "}
            <span className="profile-team-count">
              {data.direct_reports.length}
            </span>
          </h2>
          {data.direct_reports.length ? (
            <ul className="profile-team-list">
              {data.direct_reports.map((member) => (
                <li className="profile-person-row" key={member.id}>
                  <EmployeeAvatar
                    name={member.name}
                    src={member.profile_photo_url}
                  />
                  <div>
                    <strong>{member.name}</strong>
                    {member.position && <p>{member.position}</p>}
                    {member.department && (
                      <p className="muted">{member.department}</p>
                    )}
                  </div>
                  {member.is_adapting === true && (
                    <span className="profile-adapting">Проходит адаптацию</span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="muted">
              Подчинённые пока не назначены в оргструктуре.
            </p>
          )}
        </section>
      )}
    </div>
  );
}
export default function Profile() {
  const actor = useActor();
  const allowed = ["EMPLOYEE", "MANAGER"].includes(actor.role);
  const resource = useResource<PersonalProfile>(allowed ? "/me/profile" : null);
  if (!allowed) return null;
  return (
    <>
      <PageTitle
        title={actor.role === "MANAGER" ? "Профиль и команда" : "Мой профиль"}
      />
      <ErrorBox error={resource.error} />
      {resource.loading ? (
        <Loading />
      ) : (
        !resource.error &&
        resource.data && (
          <ProfileContent
            data={resource.data}
            managerView={actor.role === "MANAGER"}
          />
        )
      )}
    </>
  );
}
