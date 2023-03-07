import pandas as pd
from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
#from openpyxl import load_workbook


# Load the Excel sheet using Pandas
df = pd.read_csv('codes.csv')

# Set up Chrome driver that Selenium can use to automate the web browser
driver = webdriver.Chrome('C:\chromedriver_win32\chromedriver.exe')

# Loop through each row of the DataFrame df
for index, row in df.iterrows():
    # Navigate to the login page of the website using Selenium
    driver.get('https://www.tboxplanet.com/junior/login.php')

    # Find the email and password input boxes using Selenium
    wait = WebDriverWait(driver, 10)
    email_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='usuario']")))
    for y in range(80):
        email_input.send_keys(Keys.BACK_SPACE)
    email_input.send_keys(row['email'])
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//button[@id='nextButton']"))).click()

    wait = WebDriverWait(driver, 10)
    email_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='clave']")))
    email_input.send_keys(row['password'])
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//button[@id='sendButton']"))).click()

    wait = WebDriverWait(driver, 10)
    email_input = wait.until(EC.presence_of_element_located((By.XPATH, "//input[@id='s1']")))
    email_input.send_keys(row['code'])
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//button[@type='button']"))).click()

    wait = WebDriverWait(driver, 10)
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "(//button[@type='button'])[3]"))).click()

    wait = WebDriverWait(driver, 10)
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//div[@id='avatarContent']/div/div/div/div/div[2]/div/div/div/img"))).click()
    
    wait = WebDriverWait(driver, 10)
    WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//button[@id='btnavatar']"))).click()

    wait = WebDriverWait(driver, 10)
    #WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.XPATH, "//li[2]/a/i"))).click()
    driver.get('https://www.tboxplanet.com/junior/logout.php')
    wait = WebDriverWait(driver, 10)
    
    """
    email_input = driver.find_element("username",'email')
    email_input.send_keys(row['email'])

    login_button = driver.find_element_by_css_selector('.btn-success')
    login_button.click()
 
    # Wait for the next page to load using Selenium
    driver.implicitly_wait(10)

    # Enter the email and password values from the Excel sheet into the input boxes using Selenium
    password_input = driver.find_element("name",'password')
    password_input.send_keys(row['password'])

    # Find the login button and click it using Selenium
    login_button = driver.find_element_by_css_selector('.btn-success')
    login_button.click()

    # Wait for the next page to load using Selenium
    driver.implicitly_wait(10)

    # Find the code input box and enter the code value from the Excel sheet using Selenium
    code_input = driver.find_element("name",'code')
    code_input.send_keys(row['code'])

    # Find the submit button and click it using Selenium
    submit_button = driver.find_element_by_css_selector('.btn-info')
    submit_button.click()
    
    # Find the logout button and click it using Selenium
    logout_button = driver.find_element_by_css_selector('.btn-danger')
    logout_button.click()
    """
# Close the browser using Selenium
driver.quit()


