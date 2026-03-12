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
    audit_log.append(f"**Target:** {seats} seats. **Valid Ballots:** {valid_votes}. "
                     f"**Droop Quota:** {quota} votes.")
    ballot_weights =[1.0] * valid_votes
    
    round_num = 1
    while len(elected) < seats and len(active_candidates) > 0:
        if len(active_candidates) <= seats - len(elected):
            for c in list(active_candidates):
                elected.append(c)
                active_candidates.remove(c)
                audit_log.append(f"**Round {round_num}:** {c} elected by default "
                                 "(remaining candidates equals remaining seats).")
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
                audit_log.append(f"**Round {round_num}:** 🟢 **{w}** hits quota and is elected! "
                                 "Surplus transfers.")
                
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
                audit_log.append(f"**Round {round_num}:** Tie for elimination. "
                                 "Checking previous rounds...")
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
            audit_log.append(f"**Round {round_num}:** 🔴 **{eliminated_cand}** "
                             f"eliminated with {min_votes:.2f} votes.")
            
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

host_url = st.secrets.get("HOST_URL", "https://pubsoc-vote.streamlit.app")
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
        if isinstance(deadline_val, str):
            deadline_dt = datetime.datetime.strptime(deadline_val, "%Y-%m-%d %H:%M:%S")
        else:
            deadline_dt = deadline_val
            
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
                c.execute("SELECT is_allowed, has_voted, otp FROM voter_status "
                          "WHERE election_id=%s AND voter_hash=%s", 
                          (election['id'], voter_hash))
                record = c.fetchone()
                
                can_proceed = False
                if election['election_type'] == 'Closed (Restricted Access)':
                    if record is None or record[0] == 0:
                        st.error("❌ Access Denied: That Email/ID is not on the authorized list. "
                                 "Contact the admin.")
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
                            
                            c.execute("SELECT 1 FROM voter_status WHERE election_id=%s AND voter_hash=%s", 
                                      (election['id'], voter_hash))
                            if c.fetchone():
                                c.execute("UPDATE voter_status SET otp=%s WHERE election_id=%s AND voter_hash=%s", 
                                          (new_otp, election['id'], voter_hash))
                            else:
                                c.execute("INSERT INTO voter_status (election_id, voter_hash, is_allowed, "
                                          "has_voted, otp) VALUES (%s, %s, 1, 0, %s)", 
                                          (election['id'], voter_hash, new_otp))
                            conn.commit()
                            
                            body = (f"Your secure One-Time Password (OTP) for '{election['title']}' is:\n\n"
                                    f"{new_otp}\n\nDo not share this code with anyone.")
                            success, msg = send_smtp_email(voter_id, f"Voting Security Code: {election['title']}", body)
                            if success:
                                st.success(f"📧 A code has been emailed to **{voter_id}**.")
                            else:
                                st.warning(f"SMTP not configured or failed ({msg}).")
                                st.info(f"**FALLBACK SIMULATION:** Your code is: **{new_otp}**")
                        
                        entered_otp = st.text_input("Enter 6-digit OTP Code:")
                        if st.button("Verify OTP"):
                            c.execute("SELECT otp FROM voter_status WHERE election_id=%s AND voter_hash=%s", 
                                      (election['id'], voter_hash))
                            db_otp = c.fetchone()
                            if db_otp and db_otp[0] == entered_otp:
                                st.session_state['otp_verified'] = True
                                st.rerun()
                            else:
                                st.error("Invalid or expired OTP.")
                    
                    if st.session_state['otp_verified']:
                        st.success("Identity verified! Your choices are encrypted and anonymous.")
                        st.info("💡 **Instructions:** Select candidates in order of preference. "
                                "Rankings are **NOT compulsory**. You may rank as many or as few "
                                "as you wish. Leave a question entirely blank to abstain.")
                        
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
                                st.error("You must make at least one selection "
                                         "across the entire ballot to submit.")
                            else:
                                receipt = generate_receipt()
                                c.execute("UPDATE voter_status SET has_voted=1, otp=NULL "
                                          "WHERE election_id=%s AND voter_hash=%s", 
                                          (election['id'], voter_hash))
                                c.execute("INSERT INTO anonymous_votes (election_id, receipt_id, ballot_json) "
                                          "VALUES (%s, %s, %s)", 
                                          (election['id'], receipt, json.dumps(ballot_dict)))
                                conn.commit()
                                st.success("✅ Vote cast successfully!")
                                st.info(f"🧾 **YOUR RECEIPT:** `{receipt}`\n\n"
                                        "Save this to verify your vote on the results page.")
                                st.session_state['otp_verified'] = False
            st.stop()

        elif action_param == "results":
            st.title(f"📊 Results: {election['title']}")
            
            if is_active and election.get('is_blindfolded') == 1:
                st.warning("🔒 **Strict Neutrality Enabled:** The results of this election "
                           "are hidden until the deadline passes.")
                st.stop()
                
            c.execute("SELECT ballot_json FROM anonymous_votes WHERE election_id=%s", (election['id'],))
            raw_ballots =[json.loads(v[0]) for v in c.fetchall()]
            
            st.metric("Total Overall Ballots Cast", len(raw_ballots))
            
            quorum = election.get('quorum', 0)
            if not is_active and quorum > 0 and len(raw_ballots) < quorum:
                st.error(f"⚠️ **QUORUM NOT MET!** This election required a minimum of {quorum} votes, "
                         f"but only received {len(raw_ballots)}. These results are provisional or "
                         "invalid according to your constitution.")
            
            for q in questions_data:
                st.divider()
                st.markdown(f"## {q['title']}")
                q_ballots =[b.get(q['id']) for b in raw_ballots if b.get(q['id']) and len(b.get(q['id'])) > 0]
                display_question_results(q, q_ballots)
            
            st.divider()
            st.markdown("### 🔍 Verify Your Receipt")
            receipt_query = st.text_input("Enter your Voter Receipt ID to verify:")
            if st.button("Verify"):
                c.execute("SELECT ballot_json FROM anonymous_votes WHERE election_id=%s AND receipt_id=%s", 
                          (election['id'], receipt_query.strip().upper()))
                res = c.fetchone()
                if res:
                    st.success("✅ Your vote is logged securely in the database!")
                    st.write("**Your Cast Ballot:**", json.loads(res[0]))
                else:
                    st.error("Receipt not found.")
            st.stop()
            
    finally:
        conn.close()


# --- MAIN ADMIN APP ---
if 'logged_in' not in st.session_state: st.session_state['logged_in'] = False

admin_user = st.secrets.get("ADMIN_USER", "PubSoc")
admin_pass = st.secrets.get("ADMIN_PASS", "randomise")

with st.sidebar:
    if not st.session_state['logged_in']:
        st.markdown("### 🔐 Admin Login")
        user = st.text_input("Username")
        pw = st.text_input("Password", type="password")
        if st.button("Login"):
            if user == admin_user and pw == admin_pass:
                st.session_state['logged_in'] = True
                st.rerun()
            else: st.error("Invalid credentials")
    else:
        st.success("✅ Logged in as Admin")
        if st.button("Logout"):
            st.session_state['logged_in'] = False
            st.rerun()

st.title("🗳️ PubSoc Secure Admin Dashboard")
if not st.session_state['logged_in']:
    st.info("Please login via the sidebar to access the platform.")
    st.stop()

conn = get_connection()
try:
    c = conn.cursor() 

    elections_df = pd.read_sql_query("SELECT * FROM elections ORDER BY id DESC", get_engine())

    # Removed sub_smtp from tabs
    sub_create, sub_clone, sub_voters, sub_turnout = st.tabs([
        "Create Election Event", "Clone Past Election", "Voter Access (Emails/IDs)", 
        "Turnout & Data Export"
    ])

    # 1. CREATE ELECTION
    with sub_create:
        st.markdown("### Election Event Settings")
        new_title = st.text_input("Event Title (e.g. Annual General Meeting 2026)")
        new_desc = st.text_area("Event Description")
        elec_type = st.radio("Access Type",["Open (Link only, anyone can vote once)", "Closed (Restricted Access)"])
        
        col1, col2 = st.columns(2)
        with col1: deadline_date = st.date_input("Deadline Date", datetime.date.today() + datetime.timedelta(days=7))
        with col2: deadline_time = st.time_input("Deadline Time", datetime.time(23, 59))
            
        st.divider()
        st.markdown("### Security & Constitutional Rules")
        col3, col4 = st.columns(2)
        with col3:
            is_blind = st.checkbox("🔒 Enable Strict Neutrality (Admin Blindfold)", 
                                   help="Hides turnout metrics and standing votes from Admins until the election ends.")
        with col4:
            q_req = st.number_input("Quorum Requirement", min_value=0, value=0, 
                                    help="Minimum total ballots required. Leave 0 for no quorum.")
            
        st.divider()
        st.markdown("### Ballot Configuration")
        st.info("Define the positions or referendums to be voted on during this event.")
        
        num_questions = st.number_input("Number of Positions / Referendums", min_value=1, max_value=20, value=1)
        
        with st.form("create_election_form"):
            questions_list =[]
            for i in range(int(num_questions)):
                st.markdown(f"#### Position/Question {i+1}")
                q_title = st.text_input("Title (e.g., President OR Referendum: Change Name?)", key=f"q_title_{i}")
                q_seats = st.number_input("Seats / Winners", min_value=1, value=1, key=f"q_seats_{i}")
                q_cands = st.text_area("Candidates/Options (Format: Name | Bio)", 
                                       placeholder="Alice | Treasurer\nBob | \nYes | \nNo |", key=f"q_cands_{i}")
                q_ron = st.checkbox("Append 'Re-Open Nominations (RON)' to this question?", value=True, key=f"q_ron_{i}")
                
                questions_list.append({"id": f"q_{i}", "title_key": f"q_title_{i}", "seats_key": f"q_seats_{i}", 
                                       "cands_key": f"q_cands_{i}", "ron_key": f"q_ron_{i}"})
                st.write("---")
                
            if st.form_submit_button("Launch Full Election Event"):
                if not new_title: 
                    st.error("Event Title is required.")
                else:
                    deadline_dt = datetime.datetime.combine(deadline_date, deadline_time)
                    final_questions =[]
                    for q_ref in questions_list:
                        q_title_val = st.session_state[q_ref['title_key']]
                        q_seats_val = st.session_state[q_ref['seats_key']]
                        q_cands_raw = st.session_state[q_ref['cands_key']]
                        q_ron_val = st.session_state[q_ref['ron_key']]
                        
                        if not q_title_val: continue
                        
                        cand_list =[{"name": p.split("|")[0].strip(), "bio": p.split("|")[1].strip() if "|" in p else ""} 
                                    for p in q_cands_raw.split("\n") if p.strip()]
                        if q_ron_val:
                            cand_list.append({"name": "Re-Open Nominations (RON)", "bio": "Restart the search."})
                            
                        final_questions.append({"id": q_ref['id'], "title": q_title_val, 
                                                "seats": q_seats_val, "candidates": cand_list})
                    
                    c.execute('''INSERT INTO elections (title, description, election_type, deadline, 
                                 questions_json, is_blindfolded, quorum)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s)''', 
                              (new_title, new_desc, elec_type, deadline_dt.strftime("%Y-%m-%d %H:%M:%S"), 
                               json.dumps(final_questions), int(is_blind), q_req))
                    conn.commit()
                    st.success("Election Event created! Go to 'Voter Access' to authorize voters.")

    # 1.5 CLONE PAST ELECTION
    with sub_clone:
        if elections_df.empty:
            st.info("No elections available to clone.")
        else:
            st.markdown("### Clone a Past Election")
            st.write("Instantly recreate an old ballot with identical positions, descriptions, and candidates, "
                     "but a new deadline. (Voter access lists are NOT cloned).")
            
            clone_choice = st.selectbox("Select Election to Clone:", elections_df['title'])
            clone_data = elections_df[elections_df['title'] == clone_choice].iloc[0]
            
            col_c1, col_c2 = st.columns(2)
            with col_c1: 
                clone_dl_date = st.date_input("New Deadline Date", datetime.date.today() + datetime.timedelta(days=7), key="c_date")
            with col_c2: 
                clone_dl_time = st.time_input("New Deadline Time", datetime.time(23, 59), key="c_time")
                
            if st.button("Duplicate Election"):
                new_dt_str = datetime.datetime.combine(clone_dl_date, clone_dl_time).strftime("%Y-%m-%d %H:%M:%S")
                new_title = f"{clone_data['title']} (Copy)"
                
                c.execute('''INSERT INTO elections (title, description, election_type, deadline, 
                                 questions_json, is_blindfolded, quorum)
                                 VALUES (%s, %s, %s, %s, %s, %s, %s)''', 
                              (new_title, clone_data['description'], clone_data['election_type'], new_dt_str, 
                               clone_data['questions_json'], int(clone_data.get('is_blindfolded', 0)), 
                               int(clone_data.get('quorum', 0))))
                conn.commit()
                st.success(f"Election duplicated as '{new_title}'!")
                time.sleep(1)
                st.rerun()

    # 2. VOTER ACCESS (EMAILS / IDs)
    with sub_voters:
        if elections_df.empty:
            st.info("Create an election first.")
        else:
            v_choice = st.selectbox("Select Election to Manage Access:", elections_df['title'])
            v_id = elections_df[elections_df['title'] == v_choice].iloc[0]['id']
            
            c.execute("SELECT COUNT(*) FROM voter_status WHERE election_id=%s AND is_allowed=1", (int(v_id),))
            auth_count = c.fetchone()[0]
            st.metric("Total Authorized Voters", auth_count)
            
            col1, col2 = st.columns(2)
            with col1:
                st.markdown("#### Add Specific Emails")
                st.info("Emails are securely hashed to guarantee anonymity and are **never** stored in plain text.")
                custom_voters = st.text_area("Paste allowed emails (One per line):")
                if st.button("Authorize Batch Voters"):
                    voters =[v.strip() for v in custom_voters.split("\n") if v.strip()]
                    for v in voters:
                        c.execute('''INSERT INTO voter_status (election_id, voter_hash, is_allowed, has_voted) 
                                     VALUES (%s, %s, 1, 0) 
                                     ON CONFLICT (election_id, voter_hash) DO NOTHING''', 
                                  (int(v_id), hash_identifier(v)))
                    conn.commit()
                    st.success(f"Added {len(voters)} voters! The metric above will update.")
                    st.rerun()
                    
            with col2:
                st.markdown("#### Generate Random IDs")
                num_ids = st.number_input("How many IDs to generate?", min_value=1, value=50)
                if st.button("Generate & Authorize IDs"):
                    new_ids =[f"VOTE-{secrets.token_hex(4).upper()}" for _ in range(num_ids)]
                    for nid in new_ids:
                        c.execute('''INSERT INTO voter_status (election_id, voter_hash, is_allowed, has_voted) 
                                     VALUES (%s, %s, 1, 0) 
                                     ON CONFLICT (election_id, voter_hash) DO NOTHING''', 
                                  (int(v_id), hash_identifier(nid)))
                    conn.commit()
                    st.success(f"Generated {num_ids} IDs.")
                    
                    csv_data = pd.DataFrame(new_ids, columns=["Voter_ID"]).to_csv(index=False).encode('utf-8')
                    st.download_button("Download Generated IDs (CSV)", csv_data, "voter_ids.csv", "text/csv")

    # 3. TURNOUT & DATA EXPORT
    with sub_turnout:
        if not elections_df.empty:
            t_choice = st.selectbox("Select Election Dashboard:", elections_df['title'], key="turnout_sel")
            t_data = elections_df[elections_df['title'] == t_choice].iloc[0]
            current_election_id = int(t_data['id'])
            
            dl_val = t_data['deadline']
            deadline_dt = datetime.datetime.strptime(dl_val, "%Y-%m-%d %H:%M:%S") if isinstance(dl_val, str) else dl_val
            is_active = datetime.datetime.now() < deadline_dt
            is_blind = t_data.get('is_blindfolded') == 1

            st.markdown("### 🔗 Shareable Links")
            vote_link = get_base_url().format(current_election_id, "vote")
            results_link = get_base_url().format(current_election_id, "results")
            st.code(vote_link, language="html")
            st.caption("Direct Link: VOTE")
            st.code(results_link, language="html")
            st.caption("Direct Link: PUBLIC RESULTS / RECEIPTS")
            
            st.divider()
            
            if is_blind and is_active:
                st.warning("🔒 **STRICT NEUTRALITY ENABLED**: Turnout metrics, standing results, "
                           "and raw data exports are completely hidden from all admins until the deadline passes.")
            else:
                st.markdown("### 📊 Turnout Statistics")
                c.execute("SELECT COUNT(*) FROM voter_status WHERE election_id=%s AND is_allowed=1", 
                          (current_election_id,))
                total_allowed = c.fetchone()[0]
                
                c.execute("SELECT COUNT(*) FROM anonymous_votes WHERE election_id=%s", (current_election_id,))
                total_voted = c.fetchone()[0]
                
                if t_data['election_type'] == 'Closed (Restricted Access)' and total_allowed > 0:
                    turnout_pct = (total_voted / total_allowed) * 100
                    st.metric("Voter Turnout", f"{turnout_pct:.1f}%", f"{total_voted} out of {total_allowed} authorized voters.")
                    st.progress(min(total_voted / total_allowed, 1.0))
                else:
                    st.metric("Total Votes Cast", total_voted)

                st.markdown("### 📥 Export Anonymized Ballots")
                c.execute("SELECT receipt_id, ballot_json FROM anonymous_votes WHERE election_id=%s", 
                          (current_election_id,))
                raw_data = c.fetchall()
                if raw_data:
                    csv_data = pd.DataFrame(raw_data, columns=["Receipt_ID", "Ballot_JSON_Format"]).to_csv(index=False).encode('utf-8')
                    st.download_button("Download Raw Ballot CSV", csv_data, f"ballots_{current_election_id}.csv", "text/csv")
                else:
                    st.info("No votes cast yet.")
            
            st.divider()
            
            if is_active:
                # --- STATELESS NUDGE EMAILS ---
                st.markdown("### 📩 Send Custom Nudge Emails")
                st.info("Because plaintext emails are completely erased from the database to protect voter anonymity, "
                        "you must paste your master email list here to send nudges. The system will securely hash them, "
                        "identify exactly who hasn't voted yet, and automatically email only them.")
                
                nudge_emails_input = st.text_area("Paste Master Email List (One per line):", key="nudge_emails")
                
                st.markdown("#### Email Content")
                default_subject = f"Reminder: Vote in {t_data['title']}"
                default_body = (f"Hello,\n\nThis is a quick reminder that you are authorized to vote in "
                                f"'{t_data['title']}'.\n\nYou have not cast your ballot yet. The election "
                                f"closes on {t_data['deadline']}.\n\nCast your anonymous vote here:\n{vote_link}\n\nThank you!")
                
                custom_subject = st.text_input("Subject Line:", value=default_subject)
                custom_body = st.text_area("Email Message:", value=default_body, height=200)

                if st.button("Send Reminder to Remaining Voters"):
                    emails =[e.strip() for e in nudge_emails_input.split('\n') if '@' in e]
                    if not emails:
                        st.warning("Please paste valid email addresses.")
                    else:
                        with st.spinner("Cross-referencing secure hashes and sending emails..."):
                            sent_count = 0
                            for em in emails:
                                em_hash = hash_identifier(em)
                                c.execute("SELECT has_voted FROM voter_status WHERE election_id=%s "
                                          "AND voter_hash=%s AND is_allowed=1", 
                                          (current_election_id, em_hash))
                                record = c.fetchone()
                                if record and record[0] == 0:
                                    # Passing the fully customized subject and body from the UI inputs
                                    success, _ = send_smtp_email(em, custom_subject, custom_body)
                                    if success: sent_count += 1
                            st.success(f"Successfully sent {sent_count} nudge emails.")
                
                st.divider()
                st.markdown("### ⚙️ Manage Election")
                st.write(f"**Current Deadline:** {t_data['deadline']}")
                new_dl_date = st.date_input("Extend/Change Date", key="edit_d")
                new_dl_time = st.time_input("Extend/Change Time", key="edit_t")
                
                col_end1, col_end2 = st.columns(2)
                with col_end1:
                    if st.button("Update Deadline"):
                        new_dt_str = datetime.datetime.combine(new_dl_date, new_dl_time).strftime("%Y-%m-%d %H:%M:%S")
                        c.execute("UPDATE elections SET deadline=%s WHERE id=%s", (new_dt_str, current_election_id))
                        conn.commit()
                        st.success("Deadline updated!")
                        st.rerun()
                
                with col_end2:
                    if st.button("🚨 End Election Now"):
                        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        c.execute("UPDATE elections SET deadline=%s WHERE id=%s", (now_str, current_election_id))
                        conn.commit()
                        st.success("Election forcefully ended! You can now broadcast the results below.")
                        time.sleep(1.5)
                        st.rerun()
            
            else:
                # --- STATELESS BROADCAST RESULTS ---
                st.markdown("### 📢 Broadcast Results")
                st.info("The election has concluded. Because emails are never stored, paste your master "
                        "email list here to securely distribute the official results.")
                broadcast_emails_input = st.text_area("Paste Master Email List (One per line):", key="broadcast_emails")
                
                if st.button("Calculate & Broadcast Results"):
                    emails =[e.strip() for e in broadcast_emails_input.split('\n') if '@' in e]
                    if not emails:
                        st.warning("Please paste valid email addresses.")
                    else:
                        c.execute("SELECT ballot_json FROM anonymous_votes WHERE election_id=%s", (current_election_id,))
                        raw_ballots =[json.loads(v[0]) for v in c.fetchall()]
                        
                        results_summary = ""
                        questions_data = json.loads(t_data['questions_json'])
                        for q in questions_data:
                            q_ballots =[b.get(q['id']) for b in raw_ballots if b.get(q['id']) and len(b.get(q['id'])) > 0]
                            cands = [cand['name'] for cand in q['candidates']]
                            _, _, elected = run_multi_winner_stv(q_ballots, cands, q['seats'])
                            results_summary += f"\n- {q['title']}: {', '.join(elected) if elected else 'No winners'}"
                        
                        with st.spinner("Calculating results and sending emails..."):
                            sent_count = 0
                            for em in emails:
                                em_hash = hash_identifier(em)
                                c.execute("SELECT 1 FROM voter_status WHERE election_id=%s AND "
                                          "voter_hash=%s AND is_allowed=1", (current_election_id, em_hash))
                                if c.fetchone():
                                    body = (f"The official results for '{t_data['title']}' are in!\n\n"
                                            f"Summary of Winners:{results_summary}\n\nView the full detailed breakdown "
                                            f"and STV transfer logs here:\n{results_link}")
                                    success, _ = send_smtp_email(em, f"Official Results: {t_data['title']}", body)
                                    if success: sent_count += 1
                            st.success(f"Results successfully broadcasted to {sent_count} valid voters!")
                
                st.divider()

            # --- DANGER ZONE ---
            st.error("Danger Zone")
            if st.button("DELETE ELECTION PERMANENTLY", key="del_btn"):
                c.execute("DELETE FROM elections WHERE id=%s", (current_election_id,))
                c.execute("DELETE FROM voter_status WHERE election_id=%s", (current_election_id,))
                c.execute("DELETE FROM anonymous_votes WHERE election_id=%s", (current_election_id,))
                conn.commit()
                st.warning(f"Election '{t_choice}' deleted.")
                time.sleep(1)
                st.rerun()

finally:
    conn.close()