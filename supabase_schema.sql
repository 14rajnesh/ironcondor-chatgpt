-- Run this once in Supabase SQL Editor.
-- This table stores one active paper iron condor and its history/snapshots.

create extension if not exists pgcrypto;

create table if not exists public.paper_iron_condor_trades (
    id uuid primary key default gen_random_uuid(),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    status text not null default 'ACTIVE',
    is_paper_trade boolean not null default true,
    underlying text not null default 'NSE:NIFTY50-INDEX',

    expiry date not null,
    expiry_timestamp text,
    entry_time timestamptz,
    exit_time timestamptz,
    exit_reason text,

    entry_spot numeric,
    entry_vix numeric,
    lot_size integer not null default 75,

    legs jsonb not null default '{}'::jsonb,
    credit_per_unit numeric,
    credit_total numeric,
    wing_width numeric,
    max_loss_total numeric,
    breakeven_upper numeric,
    breakeven_lower numeric,

    realized_pnl_total numeric not null default 0,
    final_pnl_total numeric,

    adjustment_count integer not null default 0,
    adjustments_today integer not null default 0,
    last_adjustment_date date,

    history jsonb not null default '[]'::jsonb,
    snapshots jsonb not null default '[]'::jsonb
);

create index if not exists idx_paper_ic_status on public.paper_iron_condor_trades(status);
create index if not exists idx_paper_ic_expiry on public.paper_iron_condor_trades(expiry);
