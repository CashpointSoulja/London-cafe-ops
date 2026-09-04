-- Separate cafe namespace: do not modify the existing Relay integrations.
create table public.cafe_bot_config (
  key text primary key,
  value jsonb not null
);
create table public.cafe_bot_jobs (
  id text primary key,
  kind text not null check (kind in ('revenue','task','wins','daily','refresh')),
  payload jsonb not null,
  status text not null default 'pending' check (status in ('pending','done')),
  attempts integer not null default 0,
  next_at timestamptz not null default now(),
  lease_until timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);
create table public.cafe_bot_outbox (
  id text primary key,
  payload jsonb not null,
  status text not null default 'pending' check (status in ('pending','sent')),
  attempts integer not null default 0,
  next_at timestamptz not null default now(),
  lease_until timestamptz,
  last_error text,
  created_at timestamptz not null default now(),
  sent_at timestamptz,
  telegram_message_id bigint
);
create table public.cafe_bot_reports (
  day date primary key,
  body text not null,
  source jsonb not null,
  updated_at timestamptz not null default now()
);
create index cafe_bot_pending_jobs on public.cafe_bot_jobs(next_at,created_at) where status='pending';
create index cafe_bot_pending_outbox on public.cafe_bot_outbox(next_at,created_at) where status='pending';

alter table public.cafe_bot_config enable row level security;
alter table public.cafe_bot_jobs enable row level security;
alter table public.cafe_bot_outbox enable row level security;
alter table public.cafe_bot_reports enable row level security;
revoke all on public.cafe_bot_config,public.cafe_bot_jobs,public.cafe_bot_outbox,public.cafe_bot_reports from public,anon,authenticated;
grant all on public.cafe_bot_config,public.cafe_bot_jobs,public.cafe_bot_outbox,public.cafe_bot_reports to service_role;

create function public.cafe_bot_claim_jobs(p_limit integer default 3)
returns setof public.cafe_bot_jobs language plpgsql security invoker set search_path=public,pg_temp as $$
declare report_busy boolean;
begin
  -- Serialize only the short claim, not the remote API calls.
  perform pg_advisory_xact_lock(739204);
  select exists(select 1 from public.cafe_bot_jobs where status='pending'
    and kind in ('revenue','daily','refresh') and lease_until > now()) into report_busy;
  return query
    with candidates as (
      select id from public.cafe_bot_jobs
      where status='pending' and next_at<=now() and (lease_until is null or lease_until<now())
      and kind in ('task','wins') order by created_at limit least(greatest(p_limit,1),10)
      for update skip locked
    ), report as (
      select id from public.cafe_bot_jobs
      where not report_busy and status='pending' and next_at<=now()
      and (lease_until is null or lease_until<now()) and kind in ('revenue','daily','refresh')
      order by (kind='daily') desc,(kind='revenue') desc,created_at limit 1 for update skip locked
    )
    update public.cafe_bot_jobs j set lease_until=now()+interval '240 seconds'
    where j.id in (select id from candidates union all select id from report)
    returning j.*;
end $$;

create function public.cafe_bot_claim_outbox(p_limit integer default 5)
returns setof public.cafe_bot_outbox language sql security invoker set search_path=public,pg_temp as $$
  with candidates as (
    select id from public.cafe_bot_outbox
    where status='pending' and next_at<=now() and (lease_until is null or lease_until<now())
    order by created_at limit least(greatest(p_limit,1),10) for update skip locked
  )
  update public.cafe_bot_outbox o set lease_until=now()+interval '240 seconds'
  where o.id in (select id from candidates) returning o.*
$$;

create function public.cafe_bot_finish(p_id text,p_messages jsonb)
returns void language plpgsql security invoker set search_path=public,pg_temp as $$
begin
  perform 1 from public.cafe_bot_jobs where id=p_id for update;
  if not found then raise exception 'Job missing'; end if;
  insert into public.cafe_bot_outbox(id,payload)
    select item->>'id',item->'payload' from jsonb_array_elements(p_messages) item
    on conflict(id) do nothing;
  update public.cafe_bot_jobs set status='done',completed_at=now(),lease_until=null,last_error=null where id=p_id;
end $$;

revoke all on function public.cafe_bot_claim_jobs(integer),public.cafe_bot_claim_outbox(integer),public.cafe_bot_finish(text,jsonb) from public,anon,authenticated;
grant execute on function public.cafe_bot_claim_jobs(integer),public.cafe_bot_claim_outbox(integer),public.cafe_bot_finish(text,jsonb) to service_role;
insert into public.cafe_bot_config(key,value) values
  ('report_chat_id','null'),('report_thread_id','null'),('broadcast_start_day','"2026-09-04"');
