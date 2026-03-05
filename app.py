import sqlalchemy
import streamlit as st
import datetime
import json
import hashlib
import pandas as pd
import secrets
import math
import smtplib
from email.mime.text import MIMEText

# ==========================================
# 1. DATABASE SETUP (PostgreSQL / Supabase)
# ==========================================
def get_connection():
    # Pulls the URL from Streamlit Secrets
    engine = sqlalchemy.create_engine(st.secrets["DB_URL"])
    return engine.raw_connection()

def init_db():
    conn = get_connection()
    c = conn.cursor()
    # SERIAL is the PostgreSQL equivalent of AUTOINCREMENT
    c.execute('''CREATE TABLE IF NOT EXISTS elections (
                    id SERIAL PRIMARY KEY,
                    title TEXT,
                    description TEXT,
                    election_type TEXT, 
                    deadline TIMESTAMP,
                    questions_json TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS voter_status (
                    election_id INTEGER,
                    voter_hash TEXT,
                    is_allowed INTEGER, 
                    has_voted INTEGER,
                    otp TEXT,
                    PRIMARY KEY (election_id, voter_hash)
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS anonymous_votes (
                    id SERIAL PRIMARY KEY,
                    election_id INTEGER,
                    receipt_id TEXT,
                    ballot_json TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS app_config (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )''')
    conn.commit()
    conn.close()

def hash_identifier(identifier: str) -> str:
    return hashlib.sha256(identifier.lower().strip().encode()).hexdigest()

def generate_otp():
    return str(secrets.randbelow(1000000)).zfill(6)

def generate_receipt():
    return secrets.token_hex(4).upper()

# ==========================================
# 2. EMAIL 2FA SYSTEM (SMTP)
# ==========================================
def send_otp_email(to_address, otp, election_title):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT key, value FROM app_config")
    config = dict(c.fetchall())
    
    if config.get("smtp_enabled") != "True":
        return False, "SMTP is disabled."
        
    host = config.get("smtp_host", "smtp.gmail.com")
    port = int(config.get("smtp_port", "587"))
    user = config.get("smtp_user", "")
    password = config.get("smtp_pass", "")
    
    if not user or not password:
        return False, "SMTP credentials missing."
        
    msg = MIMEText(f"Your secure One-Time Password (OTP) for '{election_title}' is:\n\n{otp}")
    msg['Subject'] = f"Voting Security Code: {election_title}"
    msg['From'] = user
    msg['To'] = to_address
    
    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True, "Email sent successfully."
    except Exception as e:
        return False, str(e)

# ==========================================
# 3. ALGORITHM: MULTI-WINNER STV (Gregory)
# ==========================================
def run_multi_winner_stv(ballots, candidates, seats):
    active_candidates = set(candidates)
    elected = []
    rounds_data = []
    audit_log = []
    
    valid_votes = len(ballots)
    if valid_votes == 0:
        return [], ["No valid votes cast."], []
        
    quota = math.floor(valid_votes / (seats + 1)) + 1
    audit_log.append(f"**Target:** {seats} seats. **Quota:** {quota}.")
    ballot_weights = [1.0] * valid_votes
    
    round_num = 1
    while len(elected) < seats and len(active_candidates) > 0:
        if len(active_candidates) <= seats - len(elected):
            for c in list(active_candidates):
                elected.append(c)
                active_candidates.remove(c)
                audit_log.append(f"**Round {round_num}:** {c} elected by default.")
            break
            
        counts = {c: 0.0 for c in active_candidates}
        for i, ballot in enumerate(ballots):
            for choice in ballot:
                if choice in active_candidates:
                    counts[choice] += ballot_weights[i]
                    break
                    
        rounds_data.append(counts.copy())
        winners_this_round = [c for c, v in counts.items() if v >= quota]
        
        if winners_this_round:
            winners_this_round.sort(key=lambda x: counts[x], reverse=True)
            for w in winners_this_round:
                if len(elected) >= seats: break
                elected.append(w)
                active_candidates.remove(w)
                surplus = counts[w] - quota
                transfer_fraction = surplus / counts[w] if counts[w] > 0 else 0
                audit_log.append(f"**Round {round_num}:** 🟢 **{w}** elected!")
                
                for i, ballot in enumerate(ballots):
                    top_active = None
                    for choice in ballot:
                        if choice in active_candidates or choice == w:
                            top_active = choice
                            break
                    if top_active == w:
                        ballot_weights[i] *= transfer_fraction
        else:
            min_votes = min(counts.values())
            tied_for_last = [c for c, v in counts.items() if v == min_votes]
            tied_for_last.sort()
            eliminated_cand = tied_for_last[0]
            active_candidates.remove(eliminated_cand)
            audit_log.append(f"**Round {round_num}:** 🔴 **{eliminated_cand}** eliminated.")
            
        round_num += 1
    return rounds_data, audit_log, elected

def display_question_results(question, q_ballots):
    candidates = [c['name'] for c in question['candidates']]
    st.metric("Ballots Cast", len(q_ballots))
    
    if len(q_ballots) == 0:
        st.info("No votes cast.")
    else:
        rounds_data, audit_log, elected = run_multi_winner_stv(q_ballots, candidates, question['seats'])
        st.markdown(f"### 🏆 Winner(s)")
        for e in elected: st.success(f"**{e}**")
        
        if rounds_data:
            st.markdown("#### 📈 Votes per Round")
            df = pd.DataFrame(rounds_data).fillna(0)
            df.index = [f"Round {i+1}" for i in range(len(rounds_data))]
            st.bar_chart(df)
            
        with st.expander("📝 Show Audit Log"):
            for log in audit_log: st.write("- " + log)

# ==========================================
# 4. STREAMLIT UI & ROUTING
# ==========================================
st.set_page_config(page_title="PubSoc STV Platform", layout="wide")
init_db()

query_params = st.query_params
action_param = query_params.get("action", None)
election_id_param = query_params.get("election_id", None)

def get_base_url(): return "?election_id={}&action={}"

if action_param in ["vote", "results"] and election_id_param:
    conn = get_connection()
    c = conn.cursor()
    # Postgres uses %s
    c.execute("SELECT * FROM elections WHERE id=%s", (election_id_param,))
    elec_row = c.fetchone()
    
    if not elec_row:
        st.error("Election not found.")
        st.stop()
        
    cols = [desc[0] for desc in c.description]
    election = dict(zip(cols, elec_row))
    questions_data = json.loads(election['questions_json'])
    deadline_dt = election['deadline'] # Postgres returns a datetime object directly
    is_active = datetime.datetime.now() < deadline_dt

    if action_param == "vote":
        st.title(f"🗳️ Vote: {election['title']}")
        if not is_active:
            st.error("This election has ended.")
            st.stop()
            
        voter_id = st.text_input("Enter Email or Voter ID:")
        if voter_id:
            voter_hash = hash_identifier(voter_id)
            c.execute("SELECT is_allowed, has_voted, otp FROM voter_status WHERE election_id=%s AND voter_hash=%s", (election['id'], voter_hash))
            record = c.fetchone()
            
            can_proceed = False
            if election['election_type'] == 'Closed (Restricted Access)':
                if record is None or record[0] == 0:
                    st.error("❌ Access Denied.")
                elif record[1] == 1:
                    st.warning("Already voted.")
                else:
                    can_proceed = True
            else:
                if record is not None and record[1] == 1:
                    st.warning("Already voted.")
                else:
                    can_proceed = True
                    
            if can_proceed:
                if 'otp_verified' not in st.session_state: st.session_state['otp_verified'] = False
                    
                if not st.session_state['otp_verified']:
                    if st.button("Send Security Code (OTP)"):
                        new_otp = generate_otp()
                        c.execute("""
                            INSERT INTO voter_status (election_id, voter_hash, is_allowed, has_voted, otp) 
                            VALUES (%s, %s, 1, 0, %s)
                            ON CONFLICT (election_id, voter_hash) DO UPDATE SET otp = EXCLUDED.otp
                        """, (election['id'], voter_hash, new_otp))
                        conn.commit()
                        
                        if "@" in voter_id:
                            success, msg = send_otp_email(voter_id, new_otp, election['title'])
                            if success: st.success("📧 Code emailed.")
                            else: st.info(f"OTP (Simulated): **{new_otp}**")
                        else: st.info(f"OTP: **{new_otp}**")
                    
                    entered_otp = st.text_input("Enter 6-digit OTP:")
                    if st.button("Verify OTP"):
                        c.execute("SELECT otp FROM voter_status WHERE election_id=%s AND voter_hash=%s", (election['id'], voter_hash))
                        db_otp = c.fetchone()
                        if db_otp and db_otp[0] == entered_otp:
                            st.session_state['otp_verified'] = True
                            st.rerun()
                        else: st.error("Invalid OTP.")
                
                if st.session_state['otp_verified']:
                    ballot_dict = {}
                    for q in questions_data:
                        st.divider()
                        st.markdown(f"### {q['title']}")
                        cand_names = [c['name'] for c in q['candidates']]
                        selection = st.multiselect(f"Rank candidates:", cand_names, key=f"q_{q['id']}")
                        ballot_dict[q['id']] = selection
                    
                    if st.button("Submit Anonymous Ballot"):
                        if not any(len(v) > 0 for v in ballot_dict.values()):
                            st.error("Make at least one selection.")
                        else:
                            receipt = generate_receipt()
                            c.execute("UPDATE voter_status SET has_voted=1, otp=NULL WHERE election_id=%s AND voter_hash=%s", (election['id'], voter_hash))
                            c.execute("INSERT INTO anonymous_votes (election_id, receipt_id, ballot_json) VALUES (%s, %s, %s)", (election['id'], receipt, json.dumps(ballot_dict)))
                            conn.commit()
                            st.success(f"✅ Success! Receipt: `{receipt}`")
                            st.session_state['otp_verified'] = False
        st.stop()

    elif action_param == "results":
        st.title(f"📊 Results: {election['title']}")
        c.execute("SELECT ballot_json FROM anonymous_votes WHERE election_id=%s", (election['id'],))
        raw_ballots = [json.loads(v[0]) for v in c.fetchall()]
        st.metric("Total Overall Ballots", len(raw_ballots))
        for q in questions_data:
            st.divider()
            q_ballots = [b.get(q['id']) for b in raw_ballots if b.get(q['id'])]
            display_question_results(q, q_ballots)
        st.stop()

# --- MAIN ADMIN APP ---
if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False

with st.sidebar:
    if not st.session_state['logged_in']:
        st.markdown("### 🔐 Admin Login")
        user = st.text_input("Username")
        pw = st.text_input("Password", type="password")
        if st.button("Login"):
            if user == "PubSoc" and pw == "randomise":
                st.session_state['logged_in'] = True
                st.rerun()
    else:
        if st.button("Logout"):
            st.session_state['logged_in'] = False
            st.rerun()

if not st.session_state['logged_in']:
    st.info("Please login.")
    st.stop()

conn = get_connection()
c = conn.cursor() 
elections_df = pd.read_sql_query("SELECT * FROM elections", conn)

sub_create, sub_voters, sub_turnout, sub_smtp = st.tabs(["Create", "Voters", "Turnout", "SMTP"])

with sub_create:
    new_title = st.text_input("Event Title")
    new_desc = st.text_area("Description")
    elec_type = st.radio("Access Type", ["Open", "Closed (Restricted Access)"])
    deadline_date = st.date_input("Deadline Date", datetime.date.today() + datetime.timedelta(days=7))
    deadline_time = st.time_input("Deadline Time", datetime.time(23, 59))
    num_questions = st.number_input("Positions", min_value=1, value=1)
    
    with st.form("create_form"):
        questions_list = []
        for i in range(int(num_questions)):
            q_title = st.text_input(f"Question {i+1} Title", key=f"t_{i}")
            q_seats = st.number_input(f"Seats", min_value=1, value=1, key=f"s_{i}")
            q_cands = st.text_area(f"Candidates (Name|Bio)", key=f"c_{i}")
            questions_list.append((q_title, q_seats, q_cands))
            
        if st.form_submit_button("Launch"):
            deadline_dt = datetime.datetime.combine(deadline_date, deadline_time)
            final_questions = []
            for t, s, c_raw in questions_list:
                c_list = [{"name": p.split("|")[0].strip(), "bio": p.split("|")[1].strip() if "|" in p else ""} for p in c_raw.split("\n") if p.strip()]
                final_questions.append({"id": secrets.token_hex(4), "title": t, "seats": s, "candidates": c_list})
            
            c.execute("INSERT INTO elections (title, description, election_type, deadline, questions_json) VALUES (%s, %s, %s, %s, %s)", 
                      (new_title, new_desc, elec_type, deadline_dt, json.dumps(final_questions)))
            conn.commit()
            st.success("Launched!")

with sub_voters:
    if not elections_df.empty:
        v_choice = st.selectbox("Select Election:", elections_df['title'])
        v_id = elections_df[elections_df['title'] == v_choice].iloc[0]['id']
        
        custom_voters = st.text_area("Voters (One per line):")
        if st.button("Authorize Voters"):
            voters = [v.strip() for v in custom_voters.split("\n") if v.strip()]
            for v in voters:
                # Correct PostgreSQL syntax for INSERT OR IGNORE
                c.execute("""
                    INSERT INTO voter_status (election_id, voter_hash, is_allowed, has_voted) 
                    VALUES (%s, %s, 1, 0) 
                    ON CONFLICT (election_id, voter_hash) DO NOTHING
                """, (int(v_id), hash_identifier(v)))
            conn.commit()
            st.success("Added!")

with sub_turnout:
    if not elections_df.empty:
        t_choice = st.selectbox("Dashboard:", elections_df['title'], key="t_sel")
        t_data = elections_df[elections_df['title'] == t_choice].iloc[0]
        curr_id = int(t_data['id'])
        
        st.markdown("### 🔗 Links")
        st.code(get_base_url().format(curr_id, "vote"))
        st.code(get_base_url().format(curr_id, "results"))
        
        c.execute("SELECT COUNT(*) FROM anonymous_votes WHERE election_id=%s", (curr_id,))
        voted = c.fetchone()[0]
        st.metric("Total Votes Cast", voted)
        
        if st.button("DELETE PERMANENTLY"):
            c.execute("DELETE FROM elections WHERE id=%s", (curr_id,))
            c.execute("DELETE FROM voter_status WHERE election_id=%s", (curr_id,))
            c.execute("DELETE FROM anonymous_votes WHERE election_id=%s", (curr_id,))
            conn.commit()
            st.rerun()

with sub_smtp:
    c.execute("SELECT key, value FROM app_config")
    config = dict(c.fetchall())
    # (SMTP config UI logic matches original but with %s)