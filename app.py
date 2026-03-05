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
import time

# ==========================================
# 0. HELPER FUNCTIONS
# ==========================================
def get_ordinal(n):
    """Converts an integer into its ordinal representation (1 -> 1st, 2 -> 2nd)."""
    if 11 <= (n % 100) <= 13:
        return f"{n}th"
    return f"{n}" + {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')

# ==========================================
# 1. DATABASE SETUP (PostgreSQL Optimized)
# ==========================================
@st.cache_resource
def get_engine():
    return sqlalchemy.create_engine(st.secrets["DB_URL"])

def get_connection():
    return get_engine().raw_connection()

def init_db():
    conn = get_connection()
    try:
        c = conn.cursor()
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
        
        c.execute("ALTER TABLE elections ADD COLUMN IF NOT EXISTS is_blindfolded INTEGER DEFAULT 0")
        c.execute("ALTER TABLE elections ADD COLUMN IF NOT EXISTS quorum INTEGER DEFAULT 0")
        conn.commit()
    finally:
        conn.close()

def hash_identifier(identifier: str) -> str:
    return hashlib.sha256(identifier.lower().strip().encode()).hexdigest()

def generate_otp():
    return str(secrets.randbelow(1000000)).zfill(6)

def generate_receipt():
    return secrets.token_hex(4).upper()

# ==========================================
# 2. EMAIL SYSTEMS (SMTP)
# ==========================================
def get_smtp_config():
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT key, value FROM app_config")
        return dict(c.fetchall())
    finally:
        conn.close()

def send_smtp_email(to_address, subject, body):
    config = get_smtp_config()
    if config.get("smtp_enabled") != "True":
        return False, "SMTP is disabled."
        
    host = config.get("smtp_host", "smtp.gmail.com")
    port = int(config.get("smtp_port", "587"))
    user = config.get("smtp_user", "")
    password = config.get("smtp_pass", "")
    
    if not user or not password:
        return False, "SMTP credentials missing."
        
    msg = MIMEText(body)
    msg['Subject'] = subject
    msg['From'] = user
    msg['To'] = to_address
    
    try:
        with smtplib.SMTP(host, port) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(msg)
        return True, "Success"
    except Exception as e:
        return False, str(e)

# ==========================================
# 3. ALGORITHM: MULTI-WINNER STV (Gregory)
# ==========================================
def run_multi_winner_stv(ballots, candidates, seats):
    active_candidates = set(candidates)
    elected =[]
    rounds_data =[]
    audit_log =[]
    
    valid_votes = len(ballots)
    if valid_votes == 0:
        return [],["No valid votes cast for this specific question."],[]
        
    quota = math.floor(valid_votes / (seats + 1)) + 1
    audit_log.append(f"**Target:** {seats} seats. **Valid Ballots:** {valid_votes}. **Droop Quota:** {quota} votes.")
    ballot_weights =[1.0] * valid_votes
    
    round_num = 1
    while len(elected) < seats and len(active_candidates) > 0:
        if len(active_candidates) <= seats - len(elected):
            for c in list(active_candidates):
                elected.append(c)
                active_candidates.remove(c)
                audit_log.append(f"**Round {round_num}:** {c} elected by default (remaining candidates equals remaining seats).")
            break
            
        counts = {c: 0.0 for c in active_candidates}
        for i, ballot in enumerate(ballots):
            for choice in ballot:
                if choice in active_candidates:
                    counts[choice] += ballot_weights[i]
                    break
                    
        rounds_data.append(counts.copy())
        winners_this_round =[c for c, v in counts.items() if v >= quota]
        
        if winners_this_round:
            winners_this_round.sort(key=lambda x: counts[x], reverse=True)
            for w in winners_this_round:
                if len(elected) >= seats: break
                elected.append(w)
                active_candidates.remove(w)
                
                surplus = counts[w] - quota
                transfer_fraction = surplus / counts[w] if counts[w] > 0 else 0
                audit_log.append(f"**Round {round_num}:** 🟢 **{w}** hits quota and is elected! Surplus transfers.")
                
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
            tied_for_last =[c for c, v in counts.items() if v == min_votes]
            
            if len(tied_for_last) > 1 and len(rounds_data) > 1:
                audit_log.append(f"**Round {round_num}:** Tie for elimination. Checking previous rounds...")
                for past_round in reversed(rounds_data[:-1]):
                    past_counts = {c: past_round.get(c, 0) for c in tied_for_last}
                    past_min = min(past_counts.values())
                    past_tied =[c for c, v in past_counts.items() if v == past_min]
                    if len(past_tied) < len(tied_for_last):
                        tied_for_last = past_tied
                        break
            
            tied_for_last.sort()
            eliminated_cand = tied_for_last[0]
            active_candidates.remove(eliminated_cand)
            audit_log.append(f"**Round {round_num}:** 🔴 **{eliminated_cand}** eliminated with {min_votes:.2f} votes.")
            
        round_num += 1
        
    return rounds_data, audit_log, elected

def display_question_results(question, q_ballots):
    candidates = [c['name'] for c in question['candidates']]
    st.metric("Ballots Cast for this Question", len(q_ballots))
    
    if len(q_ballots) == 0:
        st.info("No votes cast for this question.")
    else:
        rounds_data, audit_log, elected = run_multi_winner_stv(q_ballots, candidates, question['seats'])
        st.markdown(f"### 🏆 Winner(s)")
        for e in elected: st.success(f"**{e}**")
        
        if len(rounds_data) > 0:
            st.markdown("#### 📈 Votes per Round")
            df = pd.DataFrame(rounds_data).fillna(0)
            df.index =[f"Round {i+1}" for i in range(len(rounds_data))]
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

host_url = st.secrets.get("HOST_URL", "https://your-app-url.streamlit.app")
def get_base_url(): 
    return host_url + "?election_id={}&action={}"

# --- DIRECT SHAREABLE LINKS ---
if action_param in ["vote", "results"] and election_id_param:
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT * FROM elections WHERE id=%s", (election_id_param,))
        elec_row = c.fetchone()
        
        if not elec_row:
            st.error("Election not found or link is invalid.")
            st.stop()
            
        cols =[desc[0] for desc in c.description]
        election = dict(zip(cols, elec_row))
        questions_data = json.loads(election['questions_json'])
        
        deadline_val = election['deadline']
        deadline_dt = datetime.datetime.strptime(deadline_val, "%Y-%m-%d %H:%M:%S") if isinstance(deadline_val, str) else deadline_val
        is_active = datetime.datetime.now() < deadline_dt

        if action_param == "vote":
            st.title(f"🗳️ Vote: {election['title']}")
            st.write(f"**Description:** {election['description']}")
            
            if not is_active:
                st.error("This election has ended. You can no longer cast a vote.")
                st.stop()
                
            voter_id = st.text_input("Enter your authorized Email or Voter ID:")
            if voter_id:
                voter_hash = hash_identifier(voter_id)
                c.execute("SELECT is_allowed, has_voted, otp FROM voter_status WHERE election_id=%s AND voter_hash=%s", (election['id'], voter_hash))
                record = c.fetchone()
                
                can_proceed = False
                if election['election_type'] == 'Closed (Restricted Access)':
                    if record is None or record[0] == 0:
                        st.error("❌ Access Denied: That Email/ID is not on the authorized list. Contact the admin.")
                    elif record[1] == 1:
                        st.warning("You have already voted in this election.")
                    else:
                        can_proceed = True
                else: 
                    if record is not None and record[1] == 1:
                        st.warning("You have already voted in this election.")
                    else:
                        can_proceed = True
                        
                if can_proceed:
                    is_email = "@" in voter_id
                    
                    if 'otp_verified' not in st.session_state: 
                        st.session_state['otp_verified'] = False
                        
                    # BYPASS OTP IF USING A GENERATED VOTER ID
                    if not is_email:
                        st.session_state['otp_verified'] = True
                        
                    if not st.session_state['otp_verified']:
                        if st.button("Send Security Code (OTP)"):
                            new_otp = generate_otp()
                            
                            c.execute("SELECT 1 FROM voter_status WHERE election_id=%s AND voter_hash=%s", (election['id'], voter_hash))
                            if c.fetchone():
                                c.execute("UPDATE voter_status SET otp=%s WHERE election_id=%s AND voter_hash=%s", (new_otp, election['id'], voter_hash))
                            else:
                                c.execute("INSERT INTO voter_status (election_id, voter_hash, is_allowed, has_voted, otp) VALUES (%s, %s, 1, 0, %s)", (election['id'], voter_hash, new_otp))
                            conn.commit()
                            
                            body = f"Your secure One-Time Password (OTP) for '{election['title']}' is:\n\n{new_otp}\n\nDo not share this code with anyone."
                            success, msg = send_smtp_email(voter_id, f"Voting Security Code: {election['title']}", body)
                            if success:
                                st.success(f"📧 A code has been emailed to **{voter_id}**.")
                            else:
                                st.warning(f"SMTP not configured or failed ({msg}).")
                                st.info(f"**FALLBACK SIMULATION:** Your code is: **{new_otp}**")
                        
                        entered_otp = st.text_input("Enter 6-digit OTP Code:")
                        if st.button("Verify OTP"):
                            c.execute("SELECT otp FROM voter_status WHERE election_id=%s AND voter_hash=%s", (election['id'], voter_hash))
                            db_otp = c.fetchone()
                            if db_otp and db_otp[0] == entered_otp:
                                st.session_state['otp_verified'] = True
                                st.rerun()
                            else:
                                st.error("Invalid or expired OTP.")
                    
                    if st.session_state['otp_verified']:
                        st.success("Identity verified! Your choices are encrypted and anonymous.")
                        st.info("💡 **Instructions:** Select candidates in order of preference. Rankings are **NOT compulsory**. You may rank as many or as few as you wish. Leave a question entirely blank to abstain.")
                        
                        ballot_dict = {}
                        for q in questions_data:
                            st.divider()
                            st.markdown(f"### {q['title']}")
                            st.caption(f"Electing {q['seats']} seat(s).")
                            
                            cand_names =[c['name'] for c in q['candidates']]
                            
                            with st.expander("View Bios / Manifestos"):
                                for cand in q['candidates']:
                                    st.markdown(f"**{cand['name']}**: {cand['bio']}")
                            
                            selection = st.multiselect(f"Rank candidates for {q['title']}:", cand_names, key=f"q_{q['id']}")
                            
                            if selection:
                                st.markdown("**Your Custom Rankings:**")
                                for i, choice in enumerate(selection):
                                    st.markdown(f"**{get_ordinal(i+1)} Choice:** {choice}")
                                ballot_dict[q['id']] = selection
                        
                        st.divider()
                        if st.button("Submit Anonymous Ballot"):
                            has_any_vote = any(len(v) > 0 for v in ballot_dict.values())
                            if not has_any_vote: 
                                st.error("You must make at least one selection across the 